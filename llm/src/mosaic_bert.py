# Copyright 2022 MosaicML Composer authors
# SPDX-License-Identifier: Apache-2.0

"""
A simple, flexible implementation of a Bert model.
Inspired by https://github.com/karpathy/minBert/blob/master/minBert/model.py
"""

import math
from typing import Any, Mapping

import torch
import torch.nn as nn
import torch.nn.functional as F
from composer.metrics.nlp import (LanguageCrossEntropy, MaskedAccuracy,
                                  Perplexity)
from composer.models.base import ComposerModel


class FlashNonCausalAttention(nn.Module):
    def __init__(self, cfg: Mapping[str, Any], device: str = None):
        super().__init__()
        try:
            from flash_attn.flash_attention import FlashMHA
        except ImportError as e:
            raise e

        self.mha = FlashMHA(
            embed_dim=cfg.d_model,
            num_heads=cfg.n_heads,
            attention_dropout=cfg.attn_pdrop,
            bias=True,
            batch_first=True,
            causal=False,
            device=device,
        )
        self.mha.out_proj._is_residual = True

    def forward(self, x, key_padding_mask):
        return self.mha(x,
                        key_padding_mask=key_padding_mask,
                        need_weights=False)


class BERTMLP(nn.Module):
    def __init__(self, cfg: Mapping[str, Any], device: str = None):
        super().__init__()
        self.mlp_up = nn.Linear(cfg.d_model,
                                cfg.mlp_ratio * cfg.d_model,
                                device=device)
        self.mlp_act = nn.GELU(approximate='none')
        self.mlp_down = nn.Linear(cfg.mlp_ratio * cfg.d_model,
                                  cfg.d_model,
                                  device=device)
        self.mlp_down._is_residual = True

    def forward(self, x):
        return self.mlp_down(self.mlp_act(self.mlp_up(x)))


class BERTBlock(nn.Module):
    def __init__(self, cfg: Mapping[str, Any], device: str = None):
        super().__init__()
        self.ln_1 = nn.LayerNorm(cfg.d_model, device=device)
        if cfg.attn_impl == 'flash':
            self.causal_attn = FlashNonCausalAttention(cfg, device)
        else:
            raise ValueError(f'Unknown attn_impl={cfg.attn_impl}')
        self.ln_2 = nn.LayerNorm(cfg.d_model, device=device)
        self.mlp = BERTMLP(cfg, device=device)
        self.resid_attn_dropout = nn.Dropout(cfg.resid_pdrop)
        self.resid_mlp_dropout = nn.Dropout(cfg.resid_pdrop)

    def forward(self,
                x: torch.Tensor,
                key_padding_mask: torch.ByteTensor = None) -> torch.Tensor:
        a = self.ln_1(x)
        b, _ = self.causal_attn(a, key_padding_mask)
        x = x + self.resid_attn_dropout(b)
        m = self.ln_2(x)
        n = self.mlp(m)
        x = x + self.resid_mlp_dropout(n)
        return x


class MosaicBert(nn.Module):
    def __init__(self, cfg: Mapping[str, Any]):
        super().__init__()
        assert cfg.name == 'mosaic_bert', f'Tried to build MosaicBert model with cfg.name={cfg.name}'
        self.cfg = cfg
        self.transformer = nn.ModuleDict(
            dict(
                wte=nn.Embedding(cfg.vocab_size, cfg.d_model, device=cfg.device),
                wpe=nn.Embedding(cfg.max_seq_len, cfg.d_model, device=cfg.device),
                emb_drop=nn.Dropout(cfg.emb_pdrop),
                blocks=nn.ModuleList([
                    BERTBlock(cfg, device=cfg.device) for _ in range(cfg.n_layers)
                ]),
                ln_f=nn.LayerNorm(cfg.d_model, device=cfg.device),
            ))
        self.lm_head = nn.Linear(cfg.d_model,
                                 cfg.vocab_size,
                                 bias=False,
                                 device=cfg.device)

        if cfg.device != 'meta':
            self.apply(self.param_init_fn)

    def forward(self,
                input_ids: torch.LongTensor,
                key_padding_mask: torch.ByteTensor = None):
        _, S = input_ids.size()
        assert (
            S <= self.cfg.max_seq_len
        ), f"Cannot forward input with seq_len={S}, this model only supports seq_len<={self.cfg.max_seq_len}"
        pos = torch.arange(0, S, dtype=torch.long,
                           device=input_ids.device).unsqueeze(0)

        tok_emb = self.transformer.wte(input_ids)
        pos_emb = self.transformer.wpe(pos)
        x = self.transformer.emb_drop(tok_emb + pos_emb)
        for block in self.transformer.blocks:
            x = block(x, key_padding_mask)
        x = self.transformer.ln_f(x)
        logits = self.lm_head(x)
        return logits

    # Param Initialization, needed for device='meta' fast initialization
    def param_init_fn(self, module):
        # Linear
        if isinstance(module, nn.Linear):
            torch.nn.init.normal_(module.weight,
                                  mean=0.0,
                                  std=self.cfg.init_std)
            if module.bias is not None:
                torch.nn.init.zeros_(module.bias)

            if getattr(module, '_is_residual', False):
                module.weight.data.normal_(
                    mean=0.0,
                    std=(self.cfg.init_std / math.sqrt(2 * self.cfg.n_layers)))

        # Embedding
        if isinstance(module, nn.Embedding):
            torch.nn.init.normal_(module.weight,
                                  mean=0.0,
                                  std=self.cfg.init_std)

        # LayerNorm
        if isinstance(module, nn.LayerNorm):
            torch.nn.init.zeros_(module.bias)
            torch.nn.init.ones_(module.weight)

    # FSDP Wrap function
    def fsdp_wrap_fn(self, module):
        return isinstance(module, BERTBlock)

    # Activation Checkpointing
    def activation_checkpointing_fn(self, module):
        return isinstance(module, BERTBlock)


class ComposerMosaicBert(ComposerModel):

    def __init__(self, cfg):
        super().__init__()
        self.model = MosaicBert(cfg)
        self.train_metrics = {
            'MaskedAccuracy': MaskedAccuracy(ignore_index=-100),
            'LanguageCrossEntropy': LanguageCrossEntropy(cfg.vocab_size),
            'Perplexity': Perplexity(),
        }
        self.eval_metrics = {
            'MaskedAccuracy': MaskedAccuracy(ignore_index=-100),
            'LanguageCrossEntropy': LanguageCrossEntropy(cfg.vocab_size),
            'Perplexity': Perplexity(),
        }

    def get_targets(self, batch):
        return batch['labels']

    def forward(self, batch):
        return self.model(batch['input_ids'],
                          key_padding_mask=batch['attention_mask'].bool())

    def eval_forward(self, batch, outputs=None):
        return outputs if outputs is not None else self.forward(batch)

    def loss(self, outputs, batch):
        targets = self.get_targets(batch)
        return F.cross_entropy(outputs.view(-1, outputs.size(-1)),
                               targets.view(-1),
                               ignore_index=-100)

    def get_metrics(self, is_train=False):
        return self.train_metrics if is_train else self.eval_metrics

    def update_metric(self, batch, outputs, metric):
        outputs = outputs.view(-1, outputs.size(-1))
        targets = self.get_targets(batch).view(-1)
        metric.update(outputs, targets)
