"""Channel selection. One config value chooses the provider."""

from __future__ import annotations

from core.channel.adapter import ChannelAdapter
from core.channel.meta_cloud import MetaCloudAdapter
from core.channel.peach import PeachAdapter

PROVIDERS = {
    "meta_cloud": MetaCloudAdapter,
    "peach": PeachAdapter,
}


def build_channel(provider: str, **credentials) -> ChannelAdapter:
    """Return the configured channel adapter.

    `provider` is a key of PROVIDERS, normally read from CHANNEL_PROVIDER.
    Credentials are passed through to the chosen adapter.
    """
    try:
        adapter_cls = PROVIDERS[provider]
    except KeyError:
        raise ValueError(
            f"unknown CHANNEL_PROVIDER {provider!r}; expected one of {sorted(PROVIDERS)}"
        ) from None
    return adapter_cls(**credentials)
