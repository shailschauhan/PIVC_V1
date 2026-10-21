"""DFSpot network definitions (TimmV2, TimmV2ST, ViT, ViTST), trimmed from DeepFakeSol.

Parameter/attribute names mirror the original so the published checkpoints load unchanged.
Backbones are created with pretrained=False: the checkpoints already hold the fine-tuned weights,
and this avoids any network download at load time.
"""
import timm
import torch
from torch import nn


class TimmV2(nn.Module):
    def __init__(self):
        super().__init__()
        self.efficientnet = timm.create_model("tf_efficientnetv2_l", pretrained=False)
        self.classifier1 = nn.Linear(1280, 1)
        del self.efficientnet.classifier
        self._avg_pooling = nn.AdaptiveAvgPool2d(1)
        self._dropout = nn.Dropout(p=0.5)

    def features(self, x):
        x = self._avg_pooling(self.efficientnet.forward_features(x))
        return x.flatten(start_dim=1)

    def forward(self, x):
        return self.classifier1(self._dropout(self.features(x)))


class ViT(nn.Module):
    def __init__(self):
        super().__init__()
        self.vit = timm.create_model("vit_large_patch16_224", pretrained=False)
        self.classifier1 = nn.Linear(1024, 1)
        del self.vit.head
        self._dropout = nn.Dropout(p=0.5)

    def features(self, x):
        x = self.vit.forward_features(x)
        # older timm returned the pre-logits class token; newer returns all tokens
        return x[:, 0] if x.ndim == 3 else x.flatten(start_dim=1)

    def forward(self, x):
        return self.classifier1(self._dropout(self.features(x)))


class _SiameseTuning(nn.Module):
    def __init__(self, feat_ext, num_feat: int):
        super().__init__()
        self.feat_ext = feat_ext()
        self.classifier = nn.Sequential(nn.BatchNorm1d(num_feat), nn.Linear(num_feat, 1))

    def forward(self, x):
        with torch.no_grad():
            x = self.feat_ext.features(x)
        return self.classifier(x)


class TimmV2ST(_SiameseTuning):
    def __init__(self):
        super().__init__(TimmV2, 1280)


class ViTST(_SiameseTuning):
    def __init__(self):
        super().__init__(ViT, 1024)


# name -> (class, checkpoint suffix)
REGISTRY = {"TimmV2": (TimmV2, "v2"), "TimmV2ST": (TimmV2ST, "v2st"), "ViT": (ViT, "vit"), "ViTST": (ViTST, "vitst")}
