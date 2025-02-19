#!/usr/bin/env python3
# Copyright (c) Facebook, Inc. and its affiliates. All Rights Reserved


import contextlib
import logging
from typing import Optional, Any, Dict

import mock
import yaml
from d2go.utils.helper import reroute_config_path
from detectron2.config import CfgNode as _CfgNode
from fvcore.common.registry import Registry

logger = logging.getLogger(__name__)


def get_cfg_diff_table(cfg, original_cfg):
    # print out the differences
    from d2go.config import CfgNode

    def _find_all_keys(obj):
        assert isinstance(obj, CfgNode)
        ret = []
        for key in sorted(obj.keys()):
            value = obj[key]
            if isinstance(value, CfgNode):
                for sub_key in _find_all_keys(value):
                    ret.append("{}.{}".format(key, sub_key))
            else:
                ret.append(key)
        return ret

    def _get_value(obj, full_key):
        for k in full_key.split("."):
            obj = obj[k]
        return obj

    all_old_keys = _find_all_keys(original_cfg)
    all_new_keys = _find_all_keys(cfg)
    assert all_old_keys == all_new_keys

    diff_table = []
    for full_key in all_new_keys:
        old_value = _get_value(original_cfg, full_key)
        new_value = _get_value(cfg, full_key)
        if old_value != new_value:
            diff_table.append([full_key, old_value, new_value])

    from tabulate import tabulate

    table = tabulate(
        diff_table,
        tablefmt="pipe",
        headers=["config key", "old value", "new value"],
    )
    return table


class CfgNode(_CfgNode):
    @classmethod
    def cast_from_other_class(cls, other_cfg):
        """Cast an instance of other CfgNode to D2Go's CfgNode (or its subclass)"""
        new_cfg = CfgNode(other_cfg)
        # copy all fields inside __dict__, this will preserve fields like __deprecated_keys__
        for k, v in other_cfg.__dict__.items():
            new_cfg.__dict__[k] = v
        return new_cfg

    def merge_from_file(self, cfg_filename: str, *args, **kwargs):
        cfg_filename = reroute_config_path(cfg_filename)
        with reroute_load_yaml_with_base():
            return super().merge_from_file(cfg_filename, *args, **kwargs)

    @staticmethod
    def load_yaml_with_base(filename: str, *args, **kwargs):
        with reroute_load_yaml_with_base():
            return _CfgNode.load_yaml_with_base(filename, *args, **kwargs)

    def merge_from_other_cfg(self, cfg_other):
        # NOTE: D24397488 changes default MODEL.FBNET_V2.ARCH_DEF from "" to [], change
        # the value to be able to load old full configs.
        # TODO: remove this by end of 2020.
        if cfg_other.get("MODEL", {}).get("FBNET_V2", {}).get("ARCH_DEF", None) == "":
            import logging

            logger = logging.getLogger(__name__)
            logger.warning(
                "Default value for MODEL.FBNET_V2.ARCH_DEF has changed to []"
            )
            cfg_other.MODEL.FBNET_V2.ARCH_DEF = []
        return super().merge_from_other_cfg(cfg_other)

    def __hash__(self):
        # dump follows alphabetical order, thus good for hash use
        return hash(self.dump())

    def get_field_or_none(self, field_path: str) -> Optional[Any]:
        """
        Reads out a value from the cfg node is set otherwise returns None
        The field path is the name of the parameter with all config groups
        names prefixed with a "." separator.
        e.g. if the config is
            MODEL:
              TEST:
                SCORE_THRESHOLD: 0.7
            Then to access the value of SCORE_THRESHOLD, this API should be called

            >> score_threshold = cfg.get_field_or_none("MODEL.TEST.SCORE_THRESHOLD")
        """
        attributes = field_path.split(".")
        path_to_last, last_attribute = attributes[:-1], attributes[-1]
        cfg_node = self
        for attribute in path_to_last:
            if not isinstance(cfg_node, _CfgNode) or attribute not in cfg_node:
                return None
            cfg_node = cfg_node[attribute]

        return (
            cfg_node[last_attribute]
            if isinstance(cfg_node, _CfgNode) and last_attribute in cfg_node
            else None
        )

    def as_flattened_dict(self) -> Dict[str, Any]:
        """
        Returns all keys from config object as a flattened dict.
        For example if the config is
            MODEL:
              TEST:
                SCORE_THRESHOLD: 0.7
                MIN_DIM_SIZE: 360
        The returned dict would be
          {
            "MODEL.TEST.SCORE_THRESHOLD": 0.7,
            "MODEL.TEST.MIN_DIM_SIZE": 360
          }
        """
        return self._as_flattened_dict()

    def _as_flattened_dict(self, prefix: str = "") -> Dict[str, Any]:
        ret = {}
        for key in sorted(self.keys()):
            value = self[key]
            key_path = f"{prefix}.{key}" if prefix else key
            if isinstance(value, CfgNode):
                ret.update(value._as_flattened_dict(key_path))
            else:
                ret[key_path] = value
        return ret


@contextlib.contextmanager
def reroute_load_yaml_with_base():
    BASE_KEY = "_BASE_"
    _safe_load = yaml.safe_load
    _unsafe_load = yaml.unsafe_load

    def mock_safe_load(f):
        cfg = _safe_load(f)
        if BASE_KEY in cfg:
            cfg[BASE_KEY] = reroute_config_path(cfg[BASE_KEY])
        return cfg

    def mock_unsafe_load(f):
        cfg = _unsafe_load(f)
        if BASE_KEY in cfg:
            cfg[BASE_KEY] = reroute_config_path(cfg[BASE_KEY])
        return cfg

    with mock.patch("yaml.safe_load", side_effect=mock_safe_load):
        with mock.patch("yaml.unsafe_load", side_effect=mock_unsafe_load):
            yield


CONFIG_SCALING_METHOD_REGISTRY = Registry("CONFIG_SCALING_METHOD")


def auto_scale_world_size(cfg, new_world_size):
    """
    Usually the config file is written for a specific number of devices, this method
    scales the config (in-place!) according to the actual world size using the
    pre-registered scaling methods specified as cfg.SOLVER.AUTO_SCALING_METHODS.

    Note for registering scaling methods:
        - The method will only be called when scaling is needed. It won't be called
            if SOLVER.REFERENCE_WORLD_SIZE is 0 or equal to target world size. Thus
            cfg.SOLVER.REFERENCE_WORLD_SIZE will always be positive.
        - The method updates cfg in-place, no return is required.
        - No need for changing SOLVER.REFERENCE_WORLD_SIZE.

    Args:
        cfg (CfgNode): original config which contains SOLVER.REFERENCE_WORLD_SIZE and
            SOLVER.AUTO_SCALING_METHODS.
        new_world_size: the target world size
    """

    old_world_size = cfg.SOLVER.REFERENCE_WORLD_SIZE
    if old_world_size == 0 or old_world_size == new_world_size:
        return cfg

    original_cfg = cfg.clone()
    frozen = original_cfg.is_frozen()
    cfg.defrost()

    assert len(cfg.SOLVER.AUTO_SCALING_METHODS) > 0, cfg.SOLVER.AUTO_SCALING_METHODS
    for scaling_method in cfg.SOLVER.AUTO_SCALING_METHODS:
        logger.info("Applying auto scaling method: {}".format(scaling_method))
        CONFIG_SCALING_METHOD_REGISTRY.get(scaling_method)(cfg, new_world_size)

    assert (
        cfg.SOLVER.REFERENCE_WORLD_SIZE == cfg.SOLVER.REFERENCE_WORLD_SIZE
    ), "Runner's scale_world_size shouldn't change SOLVER.REFERENCE_WORLD_SIZE"
    cfg.SOLVER.REFERENCE_WORLD_SIZE = new_world_size

    if frozen:
        cfg.freeze()

    table = get_cfg_diff_table(cfg, original_cfg)
    logger.info("Auto-scaled the config according to the actual world size: \n" + table)
