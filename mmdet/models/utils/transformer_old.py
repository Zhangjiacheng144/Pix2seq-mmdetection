# Copyright (c) OpenMMLab. All rights reserved.
import copy
import math
import warnings
from typing import Sequence

import torch
import torch.nn as nn
import torch.nn.functional as F
import torch.distributed as dist
from mmcv.cnn import (
    build_activation_layer,
    build_conv_layer,
    build_norm_layer,
    xavier_init,
)
from mmcv.cnn.bricks.registry import (
    ATTENTION,
    TRANSFORMER_LAYER,
    TRANSFORMER_LAYER_SEQUENCE,
)
from mmcv.cnn.bricks.transformer import (
    BaseTransformerLayer,
    MultiheadAttention,
    TransformerLayerSequence,
    build_attention,
    build_feedforward_network,
    build_transformer_layer_sequence,
)
from mmcv.runner.base_module import BaseModule, ModuleList
from mmcv.utils import to_2tuple
from torch.nn.init import normal_
from mmcv import ConfigDict
from mmdet.models.utils.builder import TRANSFORMER

try:
    from mmcv.ops.multi_scale_deform_attn import MultiScaleDeformableAttention

except ImportError:
    warnings.warn(
        "`MultiScaleDeformableAttention` in MMCV has been moved to "
        "`mmcv.ops.multi_scale_deform_attn`, please update your MMCV"
    )
    from mmcv.cnn.bricks.transformer import MultiScaleDeformableAttention


def nlc_to_nchw(x, hw_shape):
    """Convert [N, L, C] shape tensor to [N, C, H, W] shape tensor.

    Args:
        x (Tensor): The input tensor of shape [N, L, C] before conversion.
        hw_shape (Sequence[int]): The height and width of output feature map.

    Returns:
        Tensor: The output tensor of shape [N, C, H, W] after conversion.
    """
    H, W = hw_shape
    assert len(x.shape) == 3
    B, L, C = x.shape
    assert L == H * W, "The seq_len does not match H, W"
    return x.transpose(1, 2).reshape(B, C, H, W).contiguous()


def nchw_to_nlc(x):
    """Flatten [N, C, H, W] shape tensor to [N, L, C] shape tensor.

    Args:
        x (Tensor): The input tensor of shape [N, C, H, W] before conversion.

    Returns:
        Tensor: The output tensor of shape [N, L, C] after conversion.
    """
    assert len(x.shape) == 4
    return x.flatten(2).transpose(1, 2).contiguous()


class AdaptivePadding(nn.Module):
    """Applies padding to input (if needed) so that input can get fully covered
    by filter you specified. It support two modes "same" and "corner". The
    "same" mode is same with "SAME" padding mode in TensorFlow, pad zero around
    input. The "corner"  mode would pad zero to bottom right.

    Args:
        kernel_size (int | tuple): Size of the kernel:
        stride (int | tuple): Stride of the filter. Default: 1:
        dilation (int | tuple): Spacing between kernel elements.
            Default: 1
        padding (str): Support "same" and "corner", "corner" mode
            would pad zero to bottom right, and "same" mode would
            pad zero around input. Default: "corner".
    Example:
        >>> kernel_size = 16
        >>> stride = 16
        >>> dilation = 1
        >>> input = torch.rand(1, 1, 15, 17)
        >>> adap_pad = AdaptivePadding(
        >>>     kernel_size=kernel_size,
        >>>     stride=stride,
        >>>     dilation=dilation,
        >>>     padding="corner")
        >>> out = adap_pad(input)
        >>> assert (out.shape[2], out.shape[3]) == (16, 32)
        >>> input = torch.rand(1, 1, 16, 17)
        >>> out = adap_pad(input)
        >>> assert (out.shape[2], out.shape[3]) == (16, 32)
    """

    def __init__(self, kernel_size=1, stride=1, dilation=1, padding="corner"):

        super(AdaptivePadding, self).__init__()

        assert padding in ("same", "corner")

        kernel_size = to_2tuple(kernel_size)
        stride = to_2tuple(stride)
        padding = to_2tuple(padding)
        dilation = to_2tuple(dilation)

        self.padding = padding
        self.kernel_size = kernel_size
        self.stride = stride
        self.dilation = dilation

    def get_pad_shape(self, input_shape):
        input_h, input_w = input_shape
        kernel_h, kernel_w = self.kernel_size
        stride_h, stride_w = self.stride
        output_h = math.ceil(input_h / stride_h)
        output_w = math.ceil(input_w / stride_w)
        pad_h = max(
            (output_h - 1) * stride_h + (kernel_h - 1) *
            self.dilation[0] + 1 - input_h,
            0,
        )
        pad_w = max(
            (output_w - 1) * stride_w + (kernel_w - 1) *
            self.dilation[1] + 1 - input_w,
            0,
        )
        return pad_h, pad_w

    def forward(self, x):
        pad_h, pad_w = self.get_pad_shape(x.size()[-2:])
        if pad_h > 0 or pad_w > 0:
            if self.padding == "corner":
                x = F.pad(x, [0, pad_w, 0, pad_h])
            elif self.padding == "same":
                x = F.pad(
                    x, [pad_w // 2, pad_w - pad_w // 2,
                        pad_h // 2, pad_h - pad_h // 2]
                )
        return x


class PatchEmbed(BaseModule):
    """Image to Patch Embedding.

    We use a conv layer to implement PatchEmbed.

    Args:
        in_channels (int): The num of input channels. Default: 3
        embed_dims (int): The dimensions of embedding. Default: 768
        conv_type (str): The config dict for embedding
            conv layer type selection. Default: "Conv2d.
        kernel_size (int): The kernel_size of embedding conv. Default: 16.
        stride (int): The slide stride of embedding conv.
            Default: None (Would be set as `kernel_size`).
        padding (int | tuple | string ): The padding length of
            embedding conv. When it is a string, it means the mode
            of adaptive padding, support "same" and "corner" now.
            Default: "corner".
        dilation (int): The dilation rate of embedding conv. Default: 1.
        bias (bool): Bias of embed conv. Default: True.
        norm_cfg (dict, optional): Config dict for normalization layer.
            Default: None.
        input_size (int | tuple | None): The size of input, which will be
            used to calculate the out size. Only work when `dynamic_size`
            is False. Default: None.
        init_cfg (`mmcv.ConfigDict`, optional): The Config for initialization.
            Default: None.
    """

    def __init__(
        self,
        in_channels=3,
        embed_dims=768,
        conv_type="Conv2d",
        kernel_size=16,
        stride=16,
        padding="corner",
        dilation=1,
        bias=True,
        norm_cfg=None,
        input_size=None,
        init_cfg=None,
    ):
        super(PatchEmbed, self).__init__(init_cfg=init_cfg)

        self.embed_dims = embed_dims
        if stride is None:
            stride = kernel_size

        kernel_size = to_2tuple(kernel_size)
        stride = to_2tuple(stride)
        dilation = to_2tuple(dilation)

        if isinstance(padding, str):
            self.adap_padding = AdaptivePadding(
                kernel_size=kernel_size,
                stride=stride,
                dilation=dilation,
                padding=padding,
            )
            # disable the padding of conv
            padding = 0
        else:
            self.adap_padding = None
        padding = to_2tuple(padding)

        self.projection = build_conv_layer(
            dict(type=conv_type),
            in_channels=in_channels,
            out_channels=embed_dims,
            kernel_size=kernel_size,
            stride=stride,
            padding=padding,
            dilation=dilation,
            bias=bias,
        )

        if norm_cfg is not None:
            self.norm = build_norm_layer(norm_cfg, embed_dims)[1]
        else:
            self.norm = None

        if input_size:
            input_size = to_2tuple(input_size)
            # `init_out_size` would be used outside to
            # calculate the num_patches
            # when `use_abs_pos_embed` outside
            self.init_input_size = input_size
            if self.adap_padding:
                pad_h, pad_w = self.adap_padding.get_pad_shape(input_size)
                input_h, input_w = input_size
                input_h = input_h + pad_h
                input_w = input_w + pad_w
                input_size = (input_h, input_w)

            # https://pytorch.org/docs/stable/generated/torch.nn.Conv2d.html
            h_out = (
                input_size[0] + 2 * padding[0] -
                dilation[0] * (kernel_size[0] - 1) - 1
            ) // stride[0] + 1
            w_out = (
                input_size[1] + 2 * padding[1] -
                dilation[1] * (kernel_size[1] - 1) - 1
            ) // stride[1] + 1
            self.init_out_size = (h_out, w_out)
        else:
            self.init_input_size = None
            self.init_out_size = None

    def forward(self, x):
        """
        Args:
            x (Tensor): Has shape (B, C, H, W). In most case, C is 3.

        Returns:
            tuple: Contains merged results and its spatial shape.

                - x (Tensor): Has shape (B, out_h * out_w, embed_dims)
                - out_size (tuple[int]): Spatial shape of x, arrange as
                    (out_h, out_w).
        """

        if self.adap_padding:
            x = self.adap_padding(x)

        x = self.projection(x)
        out_size = (x.shape[2], x.shape[3])
        x = x.flatten(2).transpose(1, 2)
        if self.norm is not None:
            x = self.norm(x)
        return x, out_size


class PatchMerging(BaseModule):
    """Merge patch feature map.

    This layer groups feature map by kernel_size, and applies norm and linear
    layers to the grouped feature map. Our implementation uses `nn.Unfold` to
    merge patch, which is about 25% faster than original implementation.
    Instead, we need to modify pretrained models for compatibility.

    Args:
        in_channels (int): The num of input channels.
            to gets fully covered by filter and stride you specified..
            Default: True.
        out_channels (int): The num of output channels.
        kernel_size (int | tuple, optional): the kernel size in the unfold
            layer. Defaults to 2.
        stride (int | tuple, optional): the stride of the sliding blocks in the
            unfold layer. Default: None. (Would be set as `kernel_size`)
        padding (int | tuple | string ): The padding length of
            embedding conv. When it is a string, it means the mode
            of adaptive padding, support "same" and "corner" now.
            Default: "corner".
        dilation (int | tuple, optional): dilation parameter in the unfold
            layer. Default: 1.
        bias (bool, optional): Whether to add bias in linear layer or not.
            Defaults: False.
        norm_cfg (dict, optional): Config dict for normalization layer.
            Default: dict(type='LN').
        init_cfg (dict, optional): The extra config for initialization.
            Default: None.
    """

    def __init__(
        self,
        in_channels,
        out_channels,
        kernel_size=2,
        stride=None,
        padding="corner",
        dilation=1,
        bias=False,
        norm_cfg=dict(type="LN"),
        init_cfg=None,
    ):
        super().__init__(init_cfg=init_cfg)
        self.in_channels = in_channels
        self.out_channels = out_channels
        if stride:
            stride = stride
        else:
            stride = kernel_size

        kernel_size = to_2tuple(kernel_size)
        stride = to_2tuple(stride)
        dilation = to_2tuple(dilation)

        if isinstance(padding, str):
            self.adap_padding = AdaptivePadding(
                kernel_size=kernel_size,
                stride=stride,
                dilation=dilation,
                padding=padding,
            )
            # disable the padding of unfold
            padding = 0
        else:
            self.adap_padding = None

        padding = to_2tuple(padding)
        self.sampler = nn.Unfold(
            kernel_size=kernel_size, dilation=dilation, padding=padding, stride=stride
        )

        sample_dim = kernel_size[0] * kernel_size[1] * in_channels

        if norm_cfg is not None:
            self.norm = build_norm_layer(norm_cfg, sample_dim)[1]
        else:
            self.norm = None

        self.reduction = nn.Linear(sample_dim, out_channels, bias=bias)

    def forward(self, x, input_size):
        """
        Args:
            x (Tensor): Has shape (B, H*W, C_in).
            input_size (tuple[int]): The spatial shape of x, arrange as (H, W).
                Default: None.

        Returns:
            tuple: Contains merged results and its spatial shape.

                - x (Tensor): Has shape (B, Merged_H * Merged_W, C_out)
                - out_size (tuple[int]): Spatial shape of x, arrange as
                    (Merged_H, Merged_W).
        """
        B, L, C = x.shape
        assert isinstance(input_size, Sequence), (
            f"Expect " f"input_size is " f"`Sequence` " f"but get {input_size}"
        )

        H, W = input_size
        assert L == H * W, "input feature has wrong size"

        x = x.view(B, H, W, C).permute([0, 3, 1, 2])  # B, C, H, W
        # Use nn.Unfold to merge patch. About 25% faster than original method,
        # but need to modify pretrained model for compatibility

        if self.adap_padding:
            x = self.adap_padding(x)
            H, W = x.shape[-2:]

        x = self.sampler(x)
        # if kernel_size=2 and stride=2, x should has shape (B, 4*C, H/2*W/2)

        out_h = (
            H
            + 2 * self.sampler.padding[0]
            - self.sampler.dilation[0] * (self.sampler.kernel_size[0] - 1)
            - 1
        ) // self.sampler.stride[0] + 1
        out_w = (
            W
            + 2 * self.sampler.padding[1]
            - self.sampler.dilation[1] * (self.sampler.kernel_size[1] - 1)
            - 1
        ) // self.sampler.stride[1] + 1

        output_size = (out_h, out_w)
        x = x.transpose(1, 2)  # B, H/2*W/2, 4*C
        x = self.norm(x) if self.norm else x
        x = self.reduction(x)
        return x, output_size


def inverse_sigmoid(x, eps=1e-5):
    """Inverse function of sigmoid.

    Args:
        x (Tensor): The tensor to do the
            inverse.
        eps (float): EPS avoid numerical
            overflow. Defaults 1e-5.
    Returns:
        Tensor: The x has passed the inverse
            function of sigmoid, has same
            shape with input.
    """
    x = x.clamp(min=0, max=1)
    x1 = x.clamp(min=eps)
    x2 = (1 - x).clamp(min=eps)
    return torch.log(x1 / x2)


@TRANSFORMER_LAYER.register_module()
class DetrTransformerDecoderLayer(BaseTransformerLayer):
    """Implements decoder layer in DETR transformer.

    Args:
        attn_cfgs (list[`mmcv.ConfigDict`] | list[dict] | dict )):
            Configs for self_attention or cross_attention, the order
            should be consistent with it in `operation_order`. If it is
            a dict, it would be expand to the number of attention in
            `operation_order`.
        feedforward_channels (int): The hidden dimension for FFNs.
        ffn_dropout (float): Probability of an element to be zeroed
            in ffn. Default 0.0.
        operation_order (tuple[str]): The execution order of operation
            in transformer. Such as ('self_attn', 'norm', 'ffn', 'norm').
            Default：None
        act_cfg (dict): The activation config for FFNs. Default: `LN`
        norm_cfg (dict): Config dict for normalization layer.
            Default: `LN`.
        ffn_num_fcs (int): The number of fully-connected layers in FFNs.
            Default：2.
    """

    def __init__(
        self,
        attn_cfgs,
        feedforward_channels,
        ffn_dropout=0.0,
        operation_order=None,
        act_cfg=dict(type="ReLU", inplace=True),
        norm_cfg=dict(type="LN"),
        ffn_num_fcs=2,
        **kwargs,
    ):
        super(DetrTransformerDecoderLayer, self).__init__(
            attn_cfgs=attn_cfgs,
            feedforward_channels=feedforward_channels,
            ffn_dropout=ffn_dropout,
            operation_order=operation_order,
            act_cfg=act_cfg,
            norm_cfg=norm_cfg,
            ffn_num_fcs=ffn_num_fcs,
            **kwargs,
        )
        assert len(operation_order) == 6
        assert set(operation_order) == set(
            ["self_attn", "norm", "cross_attn", "ffn"])


@TRANSFORMER_LAYER_SEQUENCE.register_module()
class DetrTransformerEncoder(TransformerLayerSequence):
    """TransformerEncoder of DETR.

    Args:
        post_norm_cfg (dict): Config of last normalization layer. Default：
            `LN`. Only used when `self.pre_norm` is `True`
    """

    def __init__(self, *args, post_norm_cfg=dict(type="LN"), **kwargs):
        super(DetrTransformerEncoder, self).__init__(*args, **kwargs)
        if post_norm_cfg is not None:
            self.post_norm = (
                build_norm_layer(post_norm_cfg, self.embed_dims)[1]
                if self.pre_norm
                else None
            )
        else:
            assert not self.pre_norm, (
                f"Use prenorm in "
                f"{self.__class__.__name__},"
                f"Please specify post_norm_cfg"
            )
            self.post_norm = None

    def forward(self, *args, **kwargs):
        """Forward function for `TransformerCoder`.

        Returns:
            Tensor: forwarded results with shape [num_query, bs, embed_dims].
        """
        x = super(DetrTransformerEncoder, self).forward(*args, **kwargs)
        if self.post_norm is not None:
            x = self.post_norm(x)
        return x


@TRANSFORMER_LAYER_SEQUENCE.register_module()
class DetrTransformerDecoder(TransformerLayerSequence):
    """Implements the decoder in DETR transformer.

    Args:
        return_intermediate (bool): Whether to return intermediate outputs.
        post_norm_cfg (dict): Config of last normalization layer. Default：
            `LN`.
    """

    def __init__(
        self, *args, post_norm_cfg=dict(type="LN"), return_intermediate=False, **kwargs
    ):

        super(DetrTransformerDecoder, self).__init__(*args, **kwargs)
        self.return_intermediate = return_intermediate
        if post_norm_cfg is not None:
            self.post_norm = build_norm_layer(
                post_norm_cfg, self.embed_dims)[1]
        else:
            self.post_norm = None

    def forward(self, query, *args, **kwargs):
        """Forward function for `TransformerDecoder`.

        Args:
            query (Tensor): Input query with shape
                `(num_query, bs, embed_dims)`.

        Returns:
            Tensor: Results with shape [1, num_query, bs, embed_dims] when
                return_intermediate is `False`, otherwise it has shape
                [num_layers, num_query, bs, embed_dims].
        """
        if not self.return_intermediate:
            x = super().forward(query, *args, **kwargs)
            if self.post_norm:
                x = self.post_norm(x)[None]
            return x
        intermediate = []
        for layer in self.layers:
            query = layer(query, *args, **kwargs)
            if self.return_intermediate:
                if self.post_norm is not None:
                    intermediate.append(self.post_norm(query))
                else:
                    intermediate.append(query)
        return torch.stack(intermediate)


@TRANSFORMER.register_module()
class Transformer(BaseModule):
    """Implements the DETR transformer.

    Following the official DETR implementation, this module copy-paste
    from torch.nn.Transformer with modifications:

        * positional encodings are passed in MultiheadAttention
        * extra LN at the end of encoder is removed
        * decoder returns a stack of activations from all decoding layers

    See `paper: End-to-End Object Detection with Transformers
    <https://arxiv.org/pdf/2005.12872>`_ for details.

    Args:
        encoder (`mmcv.ConfigDict` | Dict): Config of
            TransformerEncoder. Defaults to None.
        decoder ((`mmcv.ConfigDict` | Dict)): Config of
            TransformerDecoder. Defaults to None
        init_cfg (obj:`mmcv.ConfigDict`): The Config for initialization.
            Defaults to None.
    """

    def __init__(self, encoder=None, decoder=None, init_cfg=None):
        super(Transformer, self).__init__(init_cfg=init_cfg)
        self.encoder = build_transformer_layer_sequence(encoder)
        self.decoder = build_transformer_layer_sequence(decoder)
        self.embed_dims = self.encoder.embed_dims

    def init_weights(self):
        # follow the official DETR to init parameters
        for m in self.modules():
            if hasattr(m, "weight") and m.weight.dim() > 1:
                xavier_init(m, distribution="uniform")
        self._is_init = True

    def forward(self, x, mask, query_embed, pos_embed):
        """Forward function for `Transformer`.

        Args:
            x (Tensor): Input query with shape [bs, c, h, w] where
                c = embed_dims.
            mask (Tensor): The key_padding_mask used for encoder and decoder,
                with shape [bs, h, w].
            query_embed (Tensor): The query embedding for decoder, with shape
                [num_query, c].
            pos_embed (Tensor): The positional encoding for encoder and
                decoder, with the same shape as `x`.

        Returns:
            tuple[Tensor]: results of decoder containing the following tensor.

                - out_dec: Output from decoder. If return_intermediate_dec \
                      is True output has shape [num_dec_layers, bs,
                      num_query, embed_dims], else has shape [1, bs, \
                      num_query, embed_dims].
                - memory: Output results from encoder, with shape \
                      [bs, embed_dims, h, w].
        """
        bs, c, h, w = x.shape
        # use `view` instead of `flatten` for dynamically exporting to ONNX
        x = x.view(bs, c, -1).permute(2, 0, 1)  # [bs, c, h, w] -> [h*w, bs, c]
        pos_embed = pos_embed.view(bs, c, -1).permute(2, 0, 1)
        query_embed = query_embed.unsqueeze(1).repeat(
            1, bs, 1
        )  # [num_query, dim] -> [num_query, bs, dim]
        mask = mask.view(bs, -1)  # [bs, h, w] -> [bs, h*w]
        memory = self.encoder(
            query=x,
            key=None,
            value=None,
            query_pos=pos_embed,
            query_key_padding_mask=mask,
        )
        target = torch.zeros_like(query_embed)
        # out_dec: [num_layers, num_query, bs, dim]
        out_dec = self.decoder(
            query=target,
            key=memory,
            value=memory,
            key_pos=pos_embed,
            query_pos=query_embed,
            key_padding_mask=mask,
        )
        out_dec = out_dec.transpose(1, 2)
        memory = memory.permute(1, 2, 0).reshape(bs, c, h, w)
        return out_dec, memory


@TRANSFORMER_LAYER_SEQUENCE.register_module()
class DeformableDetrTransformerDecoder(TransformerLayerSequence):
    """Implements the decoder in DETR transformer.

    Args:
        return_intermediate (bool): Whether to return intermediate outputs.
        coder_norm_cfg (dict): Config of last normalization layer. Default：
            `LN`.
    """

    def __init__(self, *args, return_intermediate=False, **kwargs):

        super(DeformableDetrTransformerDecoder, self).__init__(*args, **kwargs)
        self.return_intermediate = return_intermediate

    def forward(
        self,
        query,
        *args,
        reference_points=None,
        valid_ratios=None,
        reg_branches=None,
        **kwargs,
    ):
        """Forward function for `TransformerDecoder`.

        Args:
            query (Tensor): Input query with shape
                `(num_query, bs, embed_dims)`.
            reference_points (Tensor): The reference
                points of offset. has shape
                (bs, num_query, 4) when as_two_stage,
                otherwise has shape ((bs, num_query, 2).
            valid_ratios (Tensor): The radios of valid
                points on the feature map, has shape
                (bs, num_levels, 2)
            reg_branch: (obj:`nn.ModuleList`): Used for
                refining the regression results. Only would
                be passed when with_box_refine is True,
                otherwise would be passed a `None`.

        Returns:
            Tensor: Results with shape [1, num_query, bs, embed_dims] when
                return_intermediate is `False`, otherwise it has shape
                [num_layers, num_query, bs, embed_dims].
        """
        output = query
        intermediate = []
        intermediate_reference_points = []
        for lid, layer in enumerate(self.layers):
            if reference_points.shape[-1] == 4:
                reference_points_input = (
                    reference_points[:, :, None]
                    * torch.cat([valid_ratios, valid_ratios], -1)[:, None]
                )
            else:
                assert reference_points.shape[-1] == 2
                reference_points_input = (
                    reference_points[:, :, None] * valid_ratios[:, None]
                )
            output = layer(
                output, *args, reference_points=reference_points_input, **kwargs
            )
            output = output.permute(1, 0, 2)

            if reg_branches is not None:
                tmp = reg_branches[lid](output)
                if reference_points.shape[-1] == 4:
                    new_reference_points = tmp + \
                        inverse_sigmoid(reference_points)
                    new_reference_points = new_reference_points.sigmoid()
                else:
                    assert reference_points.shape[-1] == 2
                    new_reference_points = tmp
                    new_reference_points[..., :2] = tmp[..., :2] + inverse_sigmoid(
                        reference_points
                    )
                    new_reference_points = new_reference_points.sigmoid()
                reference_points = new_reference_points.detach()

            output = output.permute(1, 0, 2)
            if self.return_intermediate:
                intermediate.append(output)
                intermediate_reference_points.append(reference_points)

        if self.return_intermediate:
            return torch.stack(intermediate), torch.stack(intermediate_reference_points)

        return output, reference_points


@TRANSFORMER.register_module()
class DeformableDetrTransformer(Transformer):
    """Implements the DeformableDETR transformer.

    Args:
        as_two_stage (bool): Generate query from encoder features.
            Default: False.
        num_feature_levels (int): Number of feature maps from FPN:
            Default: 4.
        two_stage_num_proposals (int): Number of proposals when set
            `as_two_stage` as True. Default: 300.
    """

    def __init__(
        self,
        as_two_stage=False,
        num_feature_levels=4,
        two_stage_num_proposals=300,
        **kwargs,
    ):
        super(DeformableDetrTransformer, self).__init__(**kwargs)
        self.as_two_stage = as_two_stage
        self.num_feature_levels = num_feature_levels
        self.two_stage_num_proposals = two_stage_num_proposals
        self.embed_dims = self.encoder.embed_dims
        self.init_layers()

    def init_layers(self):
        """Initialize layers of the DeformableDetrTransformer."""
        self.level_embeds = nn.Parameter(
            torch.Tensor(self.num_feature_levels, self.embed_dims)
        )

        if self.as_two_stage:
            self.enc_output = nn.Linear(self.embed_dims, self.embed_dims)
            self.enc_output_norm = nn.LayerNorm(self.embed_dims)
            self.pos_trans = nn.Linear(
                self.embed_dims * 2, self.embed_dims * 2)
            self.pos_trans_norm = nn.LayerNorm(self.embed_dims * 2)
        else:
            self.reference_points = nn.Linear(self.embed_dims, 2)

    def init_weights(self):
        """Initialize the transformer weights."""
        for p in self.parameters():
            if p.dim() > 1:
                nn.init.xavier_uniform_(p)
        for m in self.modules():
            if isinstance(m, MultiScaleDeformableAttention):
                m.init_weights()
        if not self.as_two_stage:
            xavier_init(self.reference_points,
                        distribution="uniform", bias=0.0)
        normal_(self.level_embeds)

    def gen_encoder_output_proposals(self, memory, memory_padding_mask, spatial_shapes):
        """Generate proposals from encoded memory.

        Args:
            memory (Tensor) : The output of encoder,
                has shape (bs, num_key, embed_dim).  num_key is
                equal the number of points on feature map from
                all level.
            memory_padding_mask (Tensor): Padding mask for memory.
                has shape (bs, num_key).
            spatial_shapes (Tensor): The shape of all feature maps.
                has shape (num_level, 2).

        Returns:
            tuple: A tuple of feature map and bbox prediction.

                - output_memory (Tensor): The input of decoder,  \
                    has shape (bs, num_key, embed_dim).  num_key is \
                    equal the number of points on feature map from \
                    all levels.
                - output_proposals (Tensor): The normalized proposal \
                    after a inverse sigmoid, has shape \
                    (bs, num_keys, 4).
        """

        N, S, C = memory.shape
        proposals = []
        _cur = 0
        for lvl, (H, W) in enumerate(spatial_shapes):
            mask_flatten_ = memory_padding_mask[:, _cur: (_cur + H * W)].view(
                N, H, W, 1
            )
            valid_H = torch.sum(~mask_flatten_[:, :, 0, 0], 1)
            valid_W = torch.sum(~mask_flatten_[:, 0, :, 0], 1)

            grid_y, grid_x = torch.meshgrid(
                torch.linspace(0, H - 1, H, dtype=torch.float32,
                               device=memory.device),
                torch.linspace(0, W - 1, W, dtype=torch.float32,
                               device=memory.device),
            )
            grid = torch.cat([grid_x.unsqueeze(-1), grid_y.unsqueeze(-1)], -1)

            scale = torch.cat([valid_W.unsqueeze(-1), valid_H.unsqueeze(-1)], 1).view(
                N, 1, 1, 2
            )
            grid = (grid.unsqueeze(0).expand(N, -1, -1, -1) + 0.5) / scale
            wh = torch.ones_like(grid) * 0.05 * (2.0 ** lvl)
            proposal = torch.cat((grid, wh), -1).view(N, -1, 4)
            proposals.append(proposal)
            _cur += H * W
        output_proposals = torch.cat(proposals, 1)
        output_proposals_valid = (
            (output_proposals > 0.01) & (output_proposals < 0.99)
        ).all(-1, keepdim=True)
        output_proposals = torch.log(output_proposals / (1 - output_proposals))
        output_proposals = output_proposals.masked_fill(
            memory_padding_mask.unsqueeze(-1), float("inf")
        )
        output_proposals = output_proposals.masked_fill(
            ~output_proposals_valid, float("inf")
        )

        output_memory = memory
        output_memory = output_memory.masked_fill(
            memory_padding_mask.unsqueeze(-1), float(0)
        )
        output_memory = output_memory.masked_fill(
            ~output_proposals_valid, float(0))
        output_memory = self.enc_output_norm(self.enc_output(output_memory))
        return output_memory, output_proposals

    @staticmethod
    def get_reference_points(spatial_shapes, valid_ratios, device):
        """Get the reference points used in decoder.

        Args:
            spatial_shapes (Tensor): The shape of all
                feature maps, has shape (num_level, 2).
            valid_ratios (Tensor): The radios of valid
                points on the feature map, has shape
                (bs, num_levels, 2)
            device (obj:`device`): The device where
                reference_points should be.

        Returns:
            Tensor: reference points used in decoder, has \
                shape (bs, num_keys, num_levels, 2).
        """
        reference_points_list = []
        for lvl, (H, W) in enumerate(spatial_shapes):
            #  TODO  check this 0.5
            ref_y, ref_x = torch.meshgrid(
                torch.linspace(0.5, H - 0.5, H,
                               dtype=torch.float32, device=device),
                torch.linspace(0.5, W - 0.5, W,
                               dtype=torch.float32, device=device),
            )
            ref_y = ref_y.reshape(-1)[None] / \
                (valid_ratios[:, None, lvl, 1] * H)
            ref_x = ref_x.reshape(-1)[None] / \
                (valid_ratios[:, None, lvl, 0] * W)
            ref = torch.stack((ref_x, ref_y), -1)
            reference_points_list.append(ref)
        reference_points = torch.cat(reference_points_list, 1)
        reference_points = reference_points[:, :, None] * valid_ratios[:, None]
        return reference_points

    def get_valid_ratio(self, mask):
        """Get the valid radios of feature maps of all  level."""
        _, H, W = mask.shape
        valid_H = torch.sum(~mask[:, :, 0], 1)
        valid_W = torch.sum(~mask[:, 0, :], 1)
        valid_ratio_h = valid_H.float() / H
        valid_ratio_w = valid_W.float() / W
        valid_ratio = torch.stack([valid_ratio_w, valid_ratio_h], -1)
        return valid_ratio

    def get_proposal_pos_embed(self, proposals, num_pos_feats=128, temperature=10000):
        """Get the position embedding of proposal."""
        scale = 2 * math.pi
        dim_t = torch.arange(
            num_pos_feats, dtype=torch.float32, device=proposals.device
        )
        dim_t = temperature ** (2 * (dim_t // 2) / num_pos_feats)
        # N, L, 4
        proposals = proposals.sigmoid() * scale
        # N, L, 4, 128
        pos = proposals[:, :, :, None] / dim_t
        # N, L, 4, 64, 2
        pos = torch.stack(
            (pos[:, :, :, 0::2].sin(), pos[:, :, :, 1::2].cos()), dim=4
        ).flatten(2)
        return pos

    def forward(
        self,
        mlvl_feats,
        mlvl_masks,
        query_embed,
        mlvl_pos_embeds,
        reg_branches=None,
        cls_branches=None,
        **kwargs,
    ):
        """Forward function for `Transformer`.

        Args:
            mlvl_feats (list(Tensor)): Input queries from
                different level. Each element has shape
                [bs, embed_dims, h, w].
            mlvl_masks (list(Tensor)): The key_padding_mask from
                different level used for encoder and decoder,
                each element has shape  [bs, h, w].
            query_embed (Tensor): The query embedding for decoder,
                with shape [num_query, c].
            mlvl_pos_embeds (list(Tensor)): The positional encoding
                of feats from different level, has the shape
                 [bs, embed_dims, h, w].
            reg_branches (obj:`nn.ModuleList`): Regression heads for
                feature maps from each decoder layer. Only would
                be passed when
                `with_box_refine` is True. Default to None.
            cls_branches (obj:`nn.ModuleList`): Classification heads
                for feature maps from each decoder layer. Only would
                 be passed when `as_two_stage`
                 is True. Default to None.


        Returns:
            tuple[Tensor]: results of decoder containing the following tensor.

                - inter_states: Outputs from decoder. If
                    return_intermediate_dec is True output has shape \
                      (num_dec_layers, bs, num_query, embed_dims), else has \
                      shape (1, bs, num_query, embed_dims).
                - init_reference_out: The initial value of reference \
                    points, has shape (bs, num_queries, 4).
                - inter_references_out: The internal value of reference \
                    points in decoder, has shape \
                    (num_dec_layers, bs,num_query, embed_dims)
                - enc_outputs_class: The classification score of \
                    proposals generated from \
                    encoder's feature maps, has shape \
                    (batch, h*w, num_classes). \
                    Only would be returned when `as_two_stage` is True, \
                    otherwise None.
                - enc_outputs_coord_unact: The regression results \
                    generated from encoder's feature maps., has shape \
                    (batch, h*w, 4). Only would \
                    be returned when `as_two_stage` is True, \
                    otherwise None.
        """
        assert self.as_two_stage or query_embed is not None

        feat_flatten = []
        mask_flatten = []
        lvl_pos_embed_flatten = []
        spatial_shapes = []
        for lvl, (feat, mask, pos_embed) in enumerate(
            zip(mlvl_feats, mlvl_masks, mlvl_pos_embeds)
        ):
            bs, c, h, w = feat.shape
            spatial_shape = (h, w)
            spatial_shapes.append(spatial_shape)
            feat = feat.flatten(2).transpose(1, 2)
            mask = mask.flatten(1)
            pos_embed = pos_embed.flatten(2).transpose(1, 2)
            lvl_pos_embed = pos_embed + self.level_embeds[lvl].view(1, 1, -1)
            lvl_pos_embed_flatten.append(lvl_pos_embed)
            feat_flatten.append(feat)
            mask_flatten.append(mask)
        feat_flatten = torch.cat(feat_flatten, 1)
        mask_flatten = torch.cat(mask_flatten, 1)
        lvl_pos_embed_flatten = torch.cat(lvl_pos_embed_flatten, 1)
        spatial_shapes = torch.as_tensor(
            spatial_shapes, dtype=torch.long, device=feat_flatten.device
        )
        level_start_index = torch.cat(
            (spatial_shapes.new_zeros((1,)),
             spatial_shapes.prod(1).cumsum(0)[:-1])
        )
        valid_ratios = torch.stack([self.get_valid_ratio(m)
                                   for m in mlvl_masks], 1)

        reference_points = self.get_reference_points(
            spatial_shapes, valid_ratios, device=feat.device
        )

        feat_flatten = feat_flatten.permute(1, 0, 2)  # (H*W, bs, embed_dims)
        lvl_pos_embed_flatten = lvl_pos_embed_flatten.permute(
            1, 0, 2
        )  # (H*W, bs, embed_dims)
        memory = self.encoder(
            query=feat_flatten,
            key=None,
            value=None,
            query_pos=lvl_pos_embed_flatten,
            query_key_padding_mask=mask_flatten,
            spatial_shapes=spatial_shapes,
            reference_points=reference_points,
            level_start_index=level_start_index,
            valid_ratios=valid_ratios,
            **kwargs,
        )

        memory = memory.permute(1, 0, 2)
        bs, _, c = memory.shape
        if self.as_two_stage:
            output_memory, output_proposals = self.gen_encoder_output_proposals(
                memory, mask_flatten, spatial_shapes
            )
            enc_outputs_class = cls_branches[self.decoder.num_layers](
                output_memory)
            enc_outputs_coord_unact = (
                reg_branches[self.decoder.num_layers](
                    output_memory) + output_proposals
            )

            topk = self.two_stage_num_proposals
            topk_proposals = torch.topk(
                enc_outputs_class[..., 0], topk, dim=1)[1]
            topk_coords_unact = torch.gather(
                enc_outputs_coord_unact, 1, topk_proposals.unsqueeze(
                    -1).repeat(1, 1, 4)
            )
            topk_coords_unact = topk_coords_unact.detach()
            reference_points = topk_coords_unact.sigmoid()
            init_reference_out = reference_points
            pos_trans_out = self.pos_trans_norm(
                self.pos_trans(self.get_proposal_pos_embed(topk_coords_unact))
            )
            query_pos, query = torch.split(pos_trans_out, c, dim=2)
        else:
            query_pos, query = torch.split(query_embed, c, dim=1)
            query_pos = query_pos.unsqueeze(0).expand(bs, -1, -1)
            query = query.unsqueeze(0).expand(bs, -1, -1)
            reference_points = self.reference_points(query_pos).sigmoid()
            init_reference_out = reference_points

        # decoder
        query = query.permute(1, 0, 2)
        memory = memory.permute(1, 0, 2)
        query_pos = query_pos.permute(1, 0, 2)
        inter_states, inter_references = self.decoder(
            query=query,
            key=None,
            value=memory,
            query_pos=query_pos,
            key_padding_mask=mask_flatten,
            reference_points=reference_points,
            spatial_shapes=spatial_shapes,
            level_start_index=level_start_index,
            valid_ratios=valid_ratios,
            reg_branches=reg_branches,
            **kwargs,
        )

        inter_references_out = inter_references
        if self.as_two_stage:
            return (
                inter_states,
                init_reference_out,
                inter_references_out,
                enc_outputs_class,
                enc_outputs_coord_unact,
            )
        return inter_states, init_reference_out, inter_references_out, None, None


@TRANSFORMER.register_module()
class DynamicConv(BaseModule):
    """Implements Dynamic Convolution.

    This module generate parameters for each sample and
    use bmm to implement 1*1 convolution. Code is modified
    from the `official github repo <https://github.com/PeizeSun/
    SparseR-CNN/blob/main/projects/SparseRCNN/sparsercnn/head.py#L258>`_ .

    Args:
        in_channels (int): The input feature channel.
            Defaults to 256.
        feat_channels (int): The inner feature channel.
            Defaults to 64.
        out_channels (int, optional): The output feature channel.
            When not specified, it will be set to `in_channels`
            by default
        input_feat_shape (int): The shape of input feature.
            Defaults to 7.
        with_proj (bool): Project two-dimentional feature to
            one-dimentional feature. Default to True.
        act_cfg (dict): The activation config for DynamicConv.
        norm_cfg (dict): Config dict for normalization layer. Default
            layer normalization.
        init_cfg (obj:`mmcv.ConfigDict`): The Config for initialization.
            Default: None.
    """

    def __init__(
        self,
        in_channels=256,
        feat_channels=64,
        out_channels=None,
        input_feat_shape=7,
        with_proj=True,
        act_cfg=dict(type="ReLU", inplace=True),
        norm_cfg=dict(type="LN"),
        init_cfg=None,
    ):
        super(DynamicConv, self).__init__(init_cfg)
        self.in_channels = in_channels
        self.feat_channels = feat_channels
        self.out_channels_raw = out_channels
        self.input_feat_shape = input_feat_shape
        self.with_proj = with_proj
        self.act_cfg = act_cfg
        self.norm_cfg = norm_cfg
        self.out_channels = out_channels if out_channels else in_channels

        self.num_params_in = self.in_channels * self.feat_channels
        self.num_params_out = self.out_channels * self.feat_channels
        self.dynamic_layer = nn.Linear(
            self.in_channels, self.num_params_in + self.num_params_out
        )

        self.norm_in = build_norm_layer(norm_cfg, self.feat_channels)[1]
        self.norm_out = build_norm_layer(norm_cfg, self.out_channels)[1]

        self.activation = build_activation_layer(act_cfg)

        num_output = self.out_channels * input_feat_shape ** 2
        if self.with_proj:
            self.fc_layer = nn.Linear(num_output, self.out_channels)
            self.fc_norm = build_norm_layer(norm_cfg, self.out_channels)[1]

    def forward(self, param_feature, input_feature):
        """Forward function for `DynamicConv`.

        Args:
            param_feature (Tensor): The feature can be used
                to generate the parameter, has shape
                (num_all_proposals, in_channels).
            input_feature (Tensor): Feature that
                interact with parameters, has shape
                (num_all_proposals, in_channels, H, W).

        Returns:
            Tensor: The output feature has shape
            (num_all_proposals, out_channels).
        """
        input_feature = input_feature.flatten(2).permute(2, 0, 1)

        input_feature = input_feature.permute(1, 0, 2)
        parameters = self.dynamic_layer(param_feature)

        param_in = parameters[:, : self.num_params_in].view(
            -1, self.in_channels, self.feat_channels
        )
        param_out = parameters[:, -self.num_params_out:].view(
            -1, self.feat_channels, self.out_channels
        )

        # input_feature has shape (num_all_proposals, H*W, in_channels)
        # param_in has shape (num_all_proposals, in_channels, feat_channels)
        # feature has shape (num_all_proposals, H*W, feat_channels)
        features = torch.bmm(input_feature, param_in)
        features = self.norm_in(features)
        features = self.activation(features)

        # param_out has shape (batch_size, feat_channels, out_channels)
        features = torch.bmm(features, param_out)
        features = self.norm_out(features)
        features = self.activation(features)

        if self.with_proj:
            features = features.flatten(1)
            features = self.fc_layer(features)
            features = self.fc_norm(features)
            features = self.activation(features)

        return features


@TRANSFORMER.register_module()
class Pix2seqTransformer(Transformer):
    """Implements the DETR transformer.

    Following the official DETR implementation, this module copy-paste
    from torch.nn.Transformer with modifications:

        * positional encodings are passed in MultiheadAttention
        * extra LN at the end of encoder is removed
        * decoder returns a stack of activations from all decoding layers

    See `paper: End-to-End Object Detection with Transformers
    <https://arxiv.org/pdf/2005.12872>`_ for details.

    Args:
        encoder (`mmcv.ConfigDict` | Dict): Config of
            TransformerEncoder. Defaults to None.
        decoder ((`mmcv.ConfigDict` | Dict)): Config of
            TransformerDecoder. Defaults to None
        init_cfg (obj:`mmcv.ConfigDict`): The Config for initialization.
            Defaults to None.
    """

    def __init__(self, encoder=None, decoder=None, init_cfg=None, pred_eos=False):
        super(Pix2seqTransformer, self).__init__(
            encoder=encoder, decoder=decoder, init_cfg=init_cfg
        )
        self.pred_eos = pred_eos

    def forward(
        self,
        x,
        input_seq,
        mask,
        pos_embed,
        det_embed,
        vocal_embed,
        vocal_classifier,
        num_vocal,
        nucleus_sampling,
    ):
        """Forward function for `Transformer`.

        Args:
            x (Tensor): Input query with shape [bs, c, h, w] where
                c = embed_dims.
            mask (Tensor): The key_padding_mask used for encoder and decoder,
                with shape [bs, h, w].
            query_embed (Tensor): The query embedding for decoder, with shape
                [num_query, c].
            pos_embed (Tensor): The positional encoding for encoder and
                decoder, with the same shape as `x`.

        Returns:
            tuple[Tensor]: results of decoder containing the following tensor.

                - out_dec: Output from decoder. If return_intermediate_dec \
                      is True output has shape [num_dec_layers, bs,
                      num_query, embed_dims], else has shape [1, bs, \
                      num_query, embed_dims].
                - memory: Output results from encoder, with shape \
                      [bs, embed_dims, h, w].
        """
        bs, c, h, w = x.shape
        # use `view` instead of `flatten` for dynamically exporting to ONNX
        x = x.view(bs, c, -1).permute(2, 0, 1)  # [bs, c, h, w] -> [h*w, bs, c]
        pos_embed = pos_embed.view(bs, c, -1).permute(2, 0, 1)  # 没问题
        mask = mask.view(bs, -1)  # [bs, h, w] -> [bs, h*w] # 没问题
        memory = self.encoder(
            query=x,
            key=None,
            value=None,
            query_pos=pos_embed,  # 已对齐
            key_padding_mask=mask,
        )
        pre_kv = [
            torch.as_tensor([[], []], device=memory.device)
            for _ in range(self.decoder.num_layers)
        ]

        if self.training:
            input_embed = torch.cat(
                [
                    det_embed.weight.unsqueeze(0).repeat(bs, 1, 1),
                    vocal_embed(input_seq),
                ],
                dim=1,
            )
            input_embed = input_embed.transpose(0, 1)
            num_seq = input_embed.shape[0]
            self_attn_mask = (
                torch.triu(torch.ones((num_seq, num_seq)), diagonal=1)
                .bool()
                .to(input_embed.device)
            )
            out_dec, _ = self.decoder(
                input_embed,  # 没问题
                memory,
                memory_key_padding_mask=mask,
                pos=pos_embed,
                pre_kv_list=pre_kv,
                self_attn_mask=self_attn_mask,
            )
            out_dec = out_dec.transpose(0, 1)
            pred_seq_logits = vocal_classifier(out_dec)
        else:
            end = torch.zeros(bs).bool().to(memory.device)
            end_lens = torch.zeros(bs).long().to(memory.device)
            input_embed = det_embed.weight.unsqueeze(
                0).repeat(bs, 1, 1).transpose(0, 1)
            pred_seq_logits = []
            for seq_i in range(500):
                out_dec, pre_kv = self.decoder(
                    input_embed,
                    memory,
                    pos=pos_embed,
                    memory_key_padding_mask=mask,
                    pre_kv_list=pre_kv,
                )
                similarity = vocal_classifier(out_dec)

                if self.pred_eos:
                    is_eos = similarity[:, :, : num_vocal - 1].argmax(dim=-1)
                    stop_state = is_eos.squeeze(0).eq(num_vocal - 2)
                    end_lens += seq_i * (~end * stop_state)
                    end = (stop_state + end).bool()
                    if end.all() and seq_i > 4:
                        break

                if nucleus_sampling:
                    filtered_logits = top_k_top_p_filtering(
                        torch.squeeze(similarity), top_p=self.top_p
                    )
                    probabilities = F.softmax(filtered_logits, dim=-1)
                    pred_token = torch.multinomial(probabilities, 1).clamp(
                        max=self.num_bins + self.num_classes
                    )
                    pred_similarity = torch.zeros_like(similarity)
                    pred_similarity[:, :, pred_token] = 1.0
                    pred_token = pred_token.view(1, -1)
                    pred_seq_logits.append(pred_similarity.transpose(0, 1))
                else:
                    pred_token = similarity[:, :,
                                            : num_vocal - 2].argmax(dim=-1)
                    pred_seq_logits.append(similarity.transpose(0, 1))
                input_embed = vocal_embed(pred_token)

            if not self.pred_eos:
                end_lens = end_lens.fill_(500)
            pred_seq_logits = torch.cat(pred_seq_logits, dim=1)
            pred_seq_logits = [
                psl[:end_idx] for end_idx, psl in zip(end_lens, pred_seq_logits)
            ]

        return pred_seq_logits


class SelfPix2seqAttention(nn.Module):
    def __init__(self, dim, num_heads=8, dropout=0.0):
        super().__init__()
        self.num_heads = num_heads
        head_dim = dim // num_heads
        self.scale = head_dim ** -0.5

        self.qkv = nn.Linear(dim, dim * 3)
        self.attn_drop = nn.Dropout(dropout)
        self.proj = nn.Linear(dim, dim)

    def forward(self, x, pre_kv=None, attn_mask=None):
        N, B, C = x.shape
        qkv = (
            self.qkv(x)
            .reshape(N, B, 3, self.num_heads, C // self.num_heads)
            .permute(2, 1, 3, 0, 4)
        )
        # make torchscript happy (cannot use tensor as tuple)
        q, k, v = qkv[0], qkv[1], qkv[2]

        if not self.training:
            k = torch.cat([pre_kv[0], k], dim=2)
            v = torch.cat([pre_kv[1], v], dim=2)
            pre_kv = torch.stack([k, v], dim=0)

        attn = (q @ k.transpose(-2, -1)) * self.scale

        if attn_mask is not None:
            attn.masked_fill_(attn_mask, float("-inf"))

        attn = attn.softmax(dim=-1)
        attn = self.attn_drop(attn)

        x = (attn @ v).permute(2, 0, 1, 3).reshape(N, B, C)
        out = self.proj(x)
        return out, pre_kv


@ATTENTION.register_module()
class Pix2seqAttention(MultiheadAttention):
    def __init__(
        self,
        embed_dims,
        num_heads,
        attn_drop=0.0,
        proj_drop=0.0,
        self_attn_dropout=0.0,
        dropout_layer=dict(type="Dropout", drop_prob=0.0),
        init_cfg=None,
        batch_first=False,
        **kwargs,
    ):
        super(Pix2seqAttention, self).__init__(
            embed_dims=embed_dims,
            num_heads=num_heads,
            attn_drop=attn_drop,
            proj_drop=proj_drop,
            dropout_layer=dropout_layer,
            init_cfg=init_cfg,
            batch_first=batch_first,
            **kwargs,
        )
        del self.attn
        self.attn = SelfPix2seqAttention(
            dim=embed_dims, num_heads=num_heads, dropout=self_attn_dropout
        )

    def forward(
        self,
        query,
        pre_kv=None,
        key=None,
        value=None,
        identity=None,
        query_pos=None,
        key_pos=None,
        attn_mask=None,
        key_padding_mask=None,
        **kwargs,
    ):

        if key is None:
            key = query
        if value is None:
            value = key
        if identity is None:
            identity = query
        if key_pos is None:
            if query_pos is not None:
                # use query_pos if key_pos is not available
                if query_pos.shape == key.shape:
                    key_pos = query_pos
                else:
                    warnings.warn(
                        f"position encoding of key is"
                        f"missing in {self.__class__.__name__}."
                    )
        if query_pos is not None:
            query = query + query_pos
        if key_pos is not None:
            key = key + key_pos

        if self.batch_first:
            query = query.transpose(0, 1)
            key = key.transpose(0, 1)
            value = value.transpose(0, 1)

        out, pre_kv = self.attn(x=query, pre_kv=pre_kv, attn_mask=attn_mask)

        if self.batch_first:
            out = out.transpose(0, 1)

        out = identity + self.dropout_layer(self.proj_drop(out))
        return out, pre_kv


@TRANSFORMER_LAYER.register_module()
class Pix2seqTransformerDecoderLayer(DetrTransformerDecoderLayer):
    def __init__(
        self,
        attn_cfgs,
        feedforward_channels,
        ffn_dropout=0.0,
        operation_order=None,
        act_cfg=dict(type="ReLU", inplace=True),
        norm_cfg=dict(type="LN"),
        ffn_num_fcs=2,
        **kwargs,
    ):
        super(Pix2seqTransformerDecoderLayer, self).__init__(
            attn_cfgs=attn_cfgs,
            feedforward_channels=feedforward_channels,
            ffn_dropout=ffn_dropout,
            operation_order=operation_order,
            act_cfg=act_cfg,
            norm_cfg=norm_cfg,
            ffn_num_fcs=ffn_num_fcs,
            **kwargs,
        )

    def forward(
        self,
        query,
        key=None,
        value=None,
        query_pos=None,
        key_pos=None,
        attn_masks=None,
        query_key_padding_mask=None,
        key_padding_mask=None,
        pre_kv=None,
        **kwargs,
    ):

        norm_index = 0
        attn_index = 0
        ffn_index = 0
        identity = query
        if attn_masks is None:
            attn_masks = [None for _ in range(self.num_attn)]
        elif isinstance(attn_masks, torch.Tensor):
            attn_masks = [copy.deepcopy(attn_masks)
                          for _ in range(self.num_attn)]
            warnings.warn(
                f"Use same attn_mask in all attentions in "
                f"{self.__class__.__name__} "
            )
        else:
            assert len(attn_masks) == self.num_attn, (
                f"The length of "
                f"attn_masks {len(attn_masks)} must be equal "
                f"to the number of attention in "
                f"operation_order {self.num_attn}"
            )

        for layer in self.operation_order:
            if layer == "self_attn":
                temp_key = temp_value = query
                query, pre_kv = self.attentions[attn_index](
                    query,
                    pre_kv,
                    temp_key,
                    temp_value,
                    identity if self.pre_norm else None,
                    query_pos=query_pos,
                    key_pos=query_pos,
                    attn_mask=attn_masks[attn_index],
                    key_padding_mask=query_key_padding_mask,
                    **kwargs,
                )
                attn_index += 1
                identity = query

            elif layer == "norm":
                query = self.norms[norm_index](query)
                norm_index += 1

            elif layer == "cross_attn":
                query = self.attentions[attn_index](
                    query,
                    key,
                    value,
                    identity if self.pre_norm else None,
                    query_pos=query_pos,
                    key_pos=key_pos,
                    attn_mask=None,
                    key_padding_mask=key_padding_mask,
                    **kwargs,
                )
                attn_index += 1
                identity = query

            elif layer == "ffn":
                query = self.ffns[ffn_index](
                    query, identity if self.pre_norm else None)
                ffn_index += 1

        return query, pre_kv


@TRANSFORMER_LAYER_SEQUENCE.register_module()
class Pix2seqTransformerDecoder(TransformerLayerSequence):
    def __init__(self, *args, post_norm_cfg=None, **kwargs):
        super(Pix2seqTransformerDecoder, self).__init__(*args, **kwargs)
        print(post_norm_cfg)
        if post_norm_cfg is not None:
            self.post_norm = build_norm_layer(
                post_norm_cfg, self.embed_dims)[1]
        else:
            assert not self.pre_norm, (
                f"Use prenorm in "
                f"{self.__class__.__name__},"
                f"Please specify post_norm_cfg"
            )
            self.post_norm = None

    def forward(
        self,
        tgt,
        memory,
        memory_key_padding_mask,
        pos,
        pre_kv_list=None,
        self_attn_mask=None,
    ):
        output = tgt
        cur_kv_list = []
        for layer, pre_kv in zip(self.layers, pre_kv_list):
            output, cur_kv = layer(
                output,
                memory,
                memory_key_padding_mask=memory_key_padding_mask,
                key_pos=pos,
                attn_masks=self_attn_mask,
                pre_kv=pre_kv,
            )
            cur_kv_list.append(cur_kv)

        if self.post_norm is not None:
            output = self.post_norm(output)

        return output, cur_kv_list


@TRANSFORMER.register_module()
class Pix2seqTransformerPro(Pix2seqTransformer):
    def __init__(self, encoder=None, decoder=None, init_cfg=None, pred_eos=True):
        super(Pix2seqTransformerPro, self).__init__(
            encoder=encoder, decoder=decoder, init_cfg=init_cfg, pred_eos=pred_eos
        )

    def forward(
        self,
        x,
        input_seq,
        mask,
        pos_embed,
        det_embed,
        vocal_embed,
        vocal_classifier,
        num_vocal,
        nucleus_sampling,
    ):
        bs, c, h, w = x.shape
        # use `view` instead of `flatten` for dynamically exporting to ONNX
        x = x.view(bs, c, -1).permute(2, 0, 1)  # [bs, c, h, w] -> [h*w, bs, c]
        pos_embed = pos_embed.view(bs, c, -1).permute(2, 0, 1)  # 没问题
        mask = mask.view(bs, -1)  # [bs, h, w] -> [bs, h*w] # 没问题
        memory = self.encoder(
            query=x,
            key=None,
            value=None,
            query_pos=pos_embed,  # 已对齐
            key_padding_mask=mask,
        )
        pre_kv = [
            torch.as_tensor([[], []], device=memory.device)
            for _ in range(self.decoder.num_layers)
        ]

        if self.training:
            input_embed = torch.cat(
                [
                    det_embed.weight.unsqueeze(0).repeat(bs, 1, 1),
                    vocal_embed(input_seq),
                ],
                dim=1,
            )
            input_embed = input_embed.transpose(0, 1)
            num_seq = input_embed.shape[0]
            self_attn_mask = (
                torch.triu(torch.ones((num_seq, num_seq)), diagonal=1)
                .bool()
                .to(input_embed.device)
            )
            out_dec, _ = self.decoder(
                input_embed,  # 没问题
                memory,
                memory_key_padding_mask=mask,
                pos=pos_embed,
                pre_kv_list=pre_kv,
                self_attn_mask=self_attn_mask,
            )
            out_dec = out_dec.transpose(0, 1)
            pred_seq_logits = vocal_classifier(out_dec)
        else:
            end = torch.zeros(bs).bool().to(memory.device)
            end_lens = torch.zeros(bs).long().to(memory.device)
            input_embed = det_embed.weight.unsqueeze(
                0).repeat(bs, 1, 1).transpose(0, 1)
            pred_seq_logits = []
            for seq_i in range(500):
                out_dec, pre_kv = self.decoder(
                    input_embed,
                    memory,
                    pos=pos_embed,
                    memory_key_padding_mask=mask,
                    pre_kv_list=pre_kv,
                )
                similarity = vocal_classifier(out_dec)

                if self.pred_eos:
                    is_eos = similarity[:, :, : num_vocal - 1].argmax(dim=-1)
                    stop_state = is_eos.squeeze(0).eq(num_vocal - 2)
                    end_lens += seq_i * (~end * stop_state)
                    end = (stop_state + end).bool()
                    if end.all() and seq_i > 4:
                        break

                if nucleus_sampling:
                    filtered_logits = top_k_top_p_filtering(
                        torch.squeeze(similarity), top_p=self.top_p
                    )
                    probabilities = F.softmax(filtered_logits, dim=-1)
                    pred_token = torch.multinomial(probabilities, 1).clamp(
                        max=self.num_bins + self.num_classes
                    )
                    pred_similarity = torch.zeros_like(similarity)
                    pred_similarity[:, :, pred_token] = 1.0
                    pred_token = pred_token.view(1, -1)
                    pred_seq_logits.append(pred_similarity.transpose(0, 1))
                else:
                    pred_token = similarity[:, :,
                                            : num_vocal - 2].argmax(dim=-1)
                    pred_seq_logits.append(similarity.transpose(0, 1))
                input_embed = vocal_embed(pred_token)

            if not self.pred_eos:
                end_lens = end_lens.fill_(500)
            pred_seq_logits = torch.cat(pred_seq_logits, dim=1)
            pred_seq_logits = [
                psl[:end_idx] for end_idx, psl in zip(end_lens, pred_seq_logits)
            ]

        return pred_seq_logits


def top_k_top_p_filtering(logits, top_k=0, top_p=0.0, filter_value=-float("Inf")):
    """Filter a distribution of logits using top-k and/or nucleus (top-p) filtering
    Args:
        logits: logits distribution shape (vocabulary size)
        top_k >0: keep only top k tokens with highest probability (top-k filtering).
        top_p >0.0: keep the top tokens with cumulative probability >= top_p (nucleus filtering).
            Nucleus filtering is described in Holtzman et al. (http://arxiv.org/abs/1904.09751)
    """
    assert (
        logits.dim() == 1
    )  # batch size 1 for now - could be updated for more but the code would be less clear
    top_k = min(top_k, logits.size(-1))  # Safety check
    if top_k > 0:
        # Remove all tokens with a probability less than the last token of the top-k
        indices_to_remove = logits < torch.topk(logits, top_k)[
            0][..., -1, None]
        logits[indices_to_remove] = filter_value
    if top_p > 0.0:
        sorted_logits, sorted_indices = torch.sort(logits, descending=True)
        cumulative_probs = torch.cumsum(
            F.softmax(sorted_logits, dim=-1), dim=-1)
        # Remove tokens with cumulative probability above the threshold
        sorted_indices_to_remove = cumulative_probs > top_p
        # Shift the indices to the right to keep also the first token above the threshold
        sorted_indices_to_remove[...,
                                 1:] = sorted_indices_to_remove[..., :-1].clone()
        sorted_indices_to_remove[..., 0] = 0
        indices_to_remove = sorted_indices[sorted_indices_to_remove]
        logits[indices_to_remove] = filter_value
    return logits


@TRANSFORMER_LAYER_SEQUENCE.register_module()
class VQVAETransformerDecoder(TransformerLayerSequence):
    """TransformerEncoder of DETR.

    Args:
        post_norm_cfg (dict): Config of last normalization layer. Default：
            `LN`. Only used when `self.pre_norm` is `True`
    """

    def __init__(self, *args, post_norm_cfg=dict(type="LN"), **kwargs):
        super(VQVAETransformerDecoder, self).__init__(*args, **kwargs)
        if post_norm_cfg is not None:
            self.post_norm = (
                build_norm_layer(post_norm_cfg, self.embed_dims)[1]
                if self.pre_norm
                else None
            )
        else:
            assert not self.pre_norm, (
                f"Use prenorm in "
                f"{self.__class__.__name__},"
                f"Please specify post_norm_cfg"
            )
            self.post_norm = None

    def forward(self,
                query,
                key,
                value,
                query_pos=None,
                key_pos=None,
                attn_masks=None,
                query_key_padding_mask=None,
                key_padding_mask=None,
                return_inter=False,
                **kwargs):

        outs = []
        for layer in self.layers:
            query = layer(
                query,
                key,
                value,
                query_pos=query_pos,
                key_pos=key_pos,
                attn_masks=attn_masks,
                query_key_padding_mask=query_key_padding_mask,
                key_padding_mask=key_padding_mask,
                **kwargs)
            outs.append(query)
        if self.post_norm is not None:
            query = self.post_norm(query)
        outs[-1] = query
        if return_inter:
            return torch.stack(outs, dim=0)
        else:
            return query


@TRANSFORMER.register_module()
class VQVAETransformer(BaseModule):
    def __init__(
        self, encoder=None, decoder=None, init_cfg=None,
        quantize=True, n_embed=512, return_inter=False,
        attn_mask=False, quantizer=None, quant_num=5
    ):
        super(VQVAETransformer, self).__init__(init_cfg=init_cfg)
        self.encoder = build_transformer_layer_sequence(encoder)
        self.decoder = build_transformer_layer_sequence(decoder)
        self.embed_dims = self.encoder.embed_dims
        self.return_inter = return_inter
        self.attn_mask = attn_mask
        if quantizer == 'GumbelQuantizer':
            self.quantize = GumbelQuantizer(
                n_embed, self.embed_dims) if quantize else None
        elif quantizer == 'DoubleGumbelQuantizer':
            self.quantize = DoubleGumbelQuantizer(
                n_embed, self.embed_dims) if quantize else None
        elif quantizer == 'CustomGumbelQuantizer':
            self.quantize = CustomGumbelQuantizer(
                n_embed, self.embed_dims, quant_num) if quantize else None
        else:
            self.quantize = VectorQuantizer(
                n_embed, self.embed_dims, 0.25) if quantize else None

    def init_weights(self):
        # follow the official DETR to init parameters
        for m in self.modules():
            if isinstance(m, nn.Embedding):
                continue
            if hasattr(m, "weight") and m.weight.dim() > 1:
                xavier_init(m, distribution="uniform")
        self._is_init = True

    def forward(self, x, mask):
        # x需要[h*w, bs, c]
        x = x.permute(1, 0, 2)
        num_seque = x.shape[0]
        attn_masks = torch.eye(num_seque, device=x.device)
        memory = self.encoder(
            query=x,  # [100, 32, 256]
            key=None,
            value=None,
            query_pos=None,
            query_key_padding_mask=mask,  # [32, 100]
            attn_masks=attn_masks if self.attn_mask else None
        )
        # out_dec: [num_layers, num_query, bs, dim]
        if self.quantize:
            # print("before:", memory.min(), memory.max())
            memory, diff, ids = self.quantize(memory)
            # print("after :", memory.min(), memory.max())
        else:
            diff, ids = None, None
        out_dec = self.decoder(
            query=memory,
            key=None,
            value=None,
            query_pos=None,
            query_key_padding_mask=mask,
            return_inter=self.return_inter,
            attn_masks=attn_masks if self.attn_mask else None
        )
        if self.return_inter:
            # [num_layers, num_query, bs, dim]
            # [6, 100, 256, 32] -> [6, 32, 100, 256]
            out_dec = out_dec.transpose(1, 2)
        else:
            out_dec = out_dec.unsqueeze(0).transpose(
                1, 2)
        return out_dec, diff, ids


class VectorQuantizer(nn.Module):
    def __init__(self, num_embeddings, embedding_dim, commitment_cost):
        super(VectorQuantizer, self).__init__()

        self._embedding_dim = embedding_dim
        self._num_embeddings = num_embeddings

        self._embedding = nn.Embedding(
            self._num_embeddings, self._embedding_dim)
        self._embedding.weight.data.uniform_(-1, 1)
        self._commitment_cost = commitment_cost

    def forward(self, inputs):
        # convert inputs from BCHW -> BHWC
        input_shape = inputs.shape

        # Flatten input
        flat_input = inputs.view(-1, self._embedding_dim)

        # Calculate distances
        distances = (
            torch.sum(flat_input ** 2, dim=1, keepdim=True)
            + torch.sum(self._embedding.weight ** 2, dim=1)
            - 2 * torch.matmul(flat_input, self._embedding.weight.t())
        )

        # Encoding
        encoding_indices = torch.argmin(distances, dim=1).unsqueeze(1)
        encodings = torch.zeros(
            encoding_indices.shape[0], self._num_embeddings, device=inputs.device
        )
        encodings.scatter_(1, encoding_indices, 1)

        # Quantize and unflatten
        quantized = torch.matmul(
            encodings, self._embedding.weight).view(input_shape)

        # Loss
        e_latent_loss = F.mse_loss(quantized.detach(), inputs)
        q_latent_loss = F.mse_loss(quantized, inputs.detach())
        loss = q_latent_loss + self._commitment_cost * e_latent_loss

        quantized = inputs + (quantized - inputs).detach()

        # convert quantized from BHWC -> BCHW
        return quantized, loss, encoding_indices


class GumbelQuantizer(nn.Module):
    def __init__(self, num_embeddings, embedding_dim, straight_through=False):
        super(GumbelQuantizer, self).__init__()

        self._embedding_dim = embedding_dim
        self._num_embeddings = num_embeddings
        self.straight_through = straight_through
        self.fc = nn.Linear(embedding_dim, num_embeddings)
        self._embedding = nn.Embedding(
            self._num_embeddings, self._embedding_dim)
        self._embedding.weight.data.uniform_(-1, 1)

    def forward(self, inputs, temp=0.9, kl_div_loss_weight=0.0):
        # convert inputs from BCHW -> BHWC
        input_shape = inputs.shape
        # Flatten input
        flat_input = inputs.view(-1, self._embedding_dim)
        logits = self.fc(flat_input)  # [6400, 512] -> [6400, 8192]
        soft_one_hot = F.gumbel_softmax(
            logits, tau=temp, dim=1, hard=self.straight_through)  # [6400, 8192]
        quantized = torch.einsum(
            'b n, n d -> b d', soft_one_hot, self._embedding.weight).view(input_shape) # [6400, 512]
        # encoding_indices = logits.argmax(dim=1)
        encoding_indices = soft_one_hot.argmax(dim=1)
        log_qy = F.log_softmax(logits, dim=-1)
        log_uniform = torch.log(torch.tensor(
            [1. / self._num_embeddings], device=inputs.device))
        loss = F.kl_div(log_uniform, log_qy, None, None,
                          'batchmean', log_target=True)
        return quantized, loss*kl_div_loss_weight, encoding_indices

class DoubleGumbelQuantizer(nn.Module):
    def __init__(self, num_embeddings, embedding_dim, straight_through=False):
        super(DoubleGumbelQuantizer, self).__init__()

        self._embedding_dim = embedding_dim
        self._num_embeddings = num_embeddings
        self.straight_through = straight_through
        self.fc1 = nn.Linear(embedding_dim, num_embeddings)
        self.fc2 = nn.Linear(embedding_dim, num_embeddings)
        self._embedding1 = nn.Embedding(
            self._num_embeddings, self._embedding_dim)
        self._embedding1.weight.data.uniform_(-1, 1)
        self._embedding2 = nn.Embedding(
            self._num_embeddings, self._embedding_dim)
        self._embedding2.weight.data.uniform_(-1, 1)
        self.fc_out = nn.Linear(embedding_dim*2, embedding_dim)

    def forward(self, inputs, temp=0.9, kl_div_loss_weight=0.0):
        # convert inputs from BCHW -> BHWC
        input_shape = inputs.shape
        # Flatten input
        flat_input = inputs.view(-1, self._embedding_dim)
        logits1 = self.fc1(flat_input)  # [6400, 512] -> [6400, 8192]
        logits2 = self.fc2(flat_input)
        soft_one_hot1 = F.gumbel_softmax(
            logits1, tau=temp, dim=1, hard=self.straight_through)  # [6400, 8192]
        soft_one_hot2 = F.gumbel_softmax(
            logits2, tau=temp, dim=1, hard=self.straight_through)
        quantized1 = torch.einsum(
            'b n, n d -> b d', soft_one_hot1, self._embedding1.weight).view(input_shape) # [6400, 512]
        quantized2 = torch.einsum(
            'b n, n d -> b d', soft_one_hot2, self._embedding2.weight).view(input_shape)
        # encoding_indices = logits.argmax(dim=1)
        encoding_indices1 = soft_one_hot1.argmax(dim=1)
        encoding_indices2 = soft_one_hot2.argmax(dim=1)
        log_qy1 = F.log_softmax(logits1, dim=-1)
        log_qy2 = F.log_softmax(logits2, dim=-1)
        log_uniform = torch.log(torch.tensor(
            [1. / self._num_embeddings], device=inputs.device))
        loss1 = F.kl_div(log_uniform, log_qy1, None, None,
                          'batchmean', log_target=True)
        loss2 = F.kl_div(log_uniform, log_qy2, None, None,
                          'batchmean', log_target=True)
        quantized = self.fc_out(torch.cat([quantized1, quantized2], dim=-1))
        encoding_indices = torch.cat([encoding_indices1, encoding_indices2])
        return quantized, (loss1+loss2)*kl_div_loss_weight, encoding_indices
    
class CustomGumbelQuantizer(nn.Module):
    def __init__(self, num_embeddings, embedding_dim, quant_num=5, straight_through=False):
        super(CustomGumbelQuantizer, self).__init__()

        self._embedding_dim = embedding_dim
        self._num_embeddings = num_embeddings
        self.straight_through = straight_through
        self.fcs = nn.ModuleList(
            [nn.Linear(embedding_dim, num_embeddings) for i in range(quant_num)]
        )
        self._embedding1 = nn.Embedding(
            self._num_embeddings, self._embedding_dim)
        self._embedding1.weight.data.uniform_(-1, 1)
        self.fc_out = nn.Linear(embedding_dim*5, embedding_dim)

    def forward(self, inputs, temp=0.9, kl_div_loss_weight=0.0):
        # convert inputs from BCHW -> BHWC
        input_shape = inputs.shape
        # Flatten input
        flat_input = inputs.view(-1, self._embedding_dim)
        log_uniform = torch.log(torch.tensor(
            [1. / self._num_embeddings], device=inputs.device))
        encoding_indices = []
        quantized = []
        loss = []
        for fc in self.fcs:
            _logits = fc(flat_input)
            _soft_one_hot = F.gumbel_softmax(
                _logits, tau=temp, dim=1, hard=self.straight_through)
            _quantized = torch.einsum(
                'b n, n d -> b d', _soft_one_hot, self._embedding1.weight).view(input_shape)
            _encoding_indices = _soft_one_hot.argmax(dim=1)
            _log_qy = F.log_softmax(_logits, dim=-1)
            _loss = F.kl_div(log_uniform, _log_qy, None, None,
                            'batchmean', log_target=True)
            quantized.append(_quantized)
            encoding_indices.append(_encoding_indices)
            loss.append(_loss)
        quantized = self.fc_out(torch.cat(quantized, dim=-1))
        encoding_indices = torch.cat(encoding_indices)
        floss = 0.
        for l in loss:
            floss += l
        return quantized, floss*kl_div_loss_weight, encoding_indices

