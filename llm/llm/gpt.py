# Copyright 2022 MosaicML Composer authors
# SPDX-License-Identifier: Apache-2.0

"""
A simple, flexible implementation of a GPT model.
Inspired by https://github.com/karpathy/minGPT/blob/master/mingpt/model.py
"""

import math
from typing import Any, Mapping
import json

import torch
import torch.nn as nn
import torch.nn.functional as F
from composer.metrics.nlp import LanguageCrossEntropy, Perplexity
from composer.models.base import ComposerModel
from flash_attn.flash_attention import FlashMHA

from .hf_flash_gpt import GPT2FlashLMHeadModel
from transformers.models.gpt2 import GPT2Config

class ComposerGPT(ComposerModel):

    def __init__(self, cfg, device='meta'):
        super().__init__()
        # load GPT2 config from standard HF model config json
        hf_config = GPT2Config.from_dict(json.loads(open(cfg.hf_config).read()))
        # build model with config
        self.model = GPT2FlashLMHeadModel(hf_config, device=device)
        self.train_metrics = {
            'LanguageCrossEntropy': LanguageCrossEntropy(hf_config.vocab_size),
            'Perplexity': Perplexity(),
        }
        self.eval_metrics = {
            'LanguageCrossEntropy': LanguageCrossEntropy(hf_config.vocab_size),
            'Perplexity': Perplexity(),
        }

    def get_targets(self, batch):
        targets = torch.roll(batch["labels"], shifts=-1)
        targets[:, -1] = -100
        return targets

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
