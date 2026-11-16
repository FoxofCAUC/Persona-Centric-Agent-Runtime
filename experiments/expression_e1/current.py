"""当前E1实验表达入口；旧core/runner保持首轮可复核，生产接入仍在E3。"""
from .repair_protocol import JSON_PROTOCOL, TEXT_PROTOCOL
from .repair_protocol import build_messages as _build, decode_output_for, model_for as _model


DEFAULT_PROTOCOL = JSON_PROTOCOL


def build_messages(material, fixtures, *, with_assets=True, protocol=DEFAULT_PROTOCOL):
    return _build(material, fixtures, protocol, with_assets=with_assets)


def decode_output(raw, material, candidates, *, protocol=DEFAULT_PROTOCOL):
    return decode_output_for(protocol, raw, material, candidates)


def model_for(plan, position, *, protocol=DEFAULT_PROTOCOL):
    return _model(plan, position | {'protocol': protocol})
