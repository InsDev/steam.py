# Generated by the protocol buffer compiler.  DO NOT EDIT!
# sources: steammessages_unified_base.proto
# plugin: python-betterproto

from dataclasses import dataclass

import betterproto


class EProtoExecutionSite(betterproto.Enum):
    Unknown = 0
    SteamClient = 2


@dataclass
class NoResponse(betterproto.Message):
    pass