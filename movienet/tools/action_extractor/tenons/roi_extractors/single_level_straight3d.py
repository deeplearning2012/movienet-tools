import torch
import torch.nn as nn

from ...core import ops


class SingleRoIStraight3DExtractor(nn.Module):
    """Extract RoI features from a single level feature map.

    If there are multiple input feature levels, each RoI is mapped to a level
    according to its scale.

    Args:
        roi_layer (dict): Specify RoI layer type and arguments.
        out_channels (int): Output channels of RoI layers.
        featmap_strides (int): Strides of input feature maps.
        finest_scale (int): Scale threshold of mapping to level 0.
    """

    def __init__(self,
                 roi_layer,
                 out_channels,
                 featmap_strides,
                 finest_scale=56,
                 with_temporal_pool=False):
        super(SingleRoIStraight3DExtractor, self).__init__()
        self.roi_layers = self.build_roi_layers(roi_layer, featmap_strides)
        self.out_channels = out_channels
        self.featmap_strides = featmap_strides
        self.finest_scale = finest_scale
        self.with_temporal_pool = with_temporal_pool

    @property
    def num_inputs(self):
        """int: Input feature map levels."""
        return len(self.featmap_strides)

    def init_weights(self):
        pass

    def build_roi_layers(self, layer_cfg, featmap_strides):
        cfg = layer_cfg.copy()
        layer_type = cfg.pop('type')
        assert hasattr(ops, layer_type)
        layer_cls = getattr(ops, layer_type)
        roi_layers = nn.ModuleList(
            [layer_cls(spatial_scale=1 / s, **cfg) for s in featmap_strides])
        return roi_layers

    def map_roi_levels(self, rois, num_levels):
        """Map rois to corresponding feature levels by scales.

        - scale < finest_scale: level 0
        - finest_scale <= scale < finest_scale * 2: level 1
        - finest_scale * 2 <= scale < finest_scale * 4: level 2
        - scale >= finest_scale * 4: level 3

        Args:
            rois (Tensor): Input RoIs, shape (k, 5).
            num_levels (int): Total level number.

        Returns:
            Tensor: Level index (0-based) of each RoI, shape (k, )
        """
        scale = torch.sqrt(
            (rois[:, 3] - rois[:, 1] + 1) * (rois[:, 4] - rois[:, 2] + 1))
        target_lvls = torch.floor(torch.log2(scale / self.finest_scale + 1e-6))
        target_lvls = target_lvls.clamp(min=0, max=num_levels - 1).long()
        return target_lvls

    def forward(self, feats, rois):
        feats = list(feats)
        if len(feats) == 1:
            if self.with_temporal_pool:
                feats[0] = torch.mean(feats[0], 2, keepdim=True)
            roi_feats = []
            for t in range(feats[0].size(2)):
                feat = feats[0][:, :, t, :, :].contiguous()
                roi_feats.append(self.roi_layers[0](feat, rois))
            return torch.stack(roi_feats, dim=2)

        if self.with_temporal_pool:
            for i in range(len(feats)):
                feats[i] = torch.mean(feats[i], 2, keepdim=True)

        t_size = feats[0].size(2)
        out_size = self.roi_layers[0].out_size
        num_levels = len(feats)
        target_lvls = self.map_roi_levels(rois, num_levels)
        roi_feats = torch.cuda.FloatTensor(rois.size()[0], self.out_channels,
                                           t_size, out_size, out_size).fill_(0)
        for i in range(num_levels):
            inds = target_lvls == i
            if inds.any():
                rois_ = rois[inds, :]
                for t in range(t_size):
                    feat_ = feats[i][:, :, t, :, :].contiguous()
                    roi_feats_t = self.roi_layers[i](feat_, rois_)
                    roi_feats[inds, :, t, :, :] += roi_feats_t
        return roi_feats