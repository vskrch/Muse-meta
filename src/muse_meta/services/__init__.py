"""Service layer for external API communication and data transformation."""

from muse_meta.services.muse_client import MuseClient, get_muse_client

__all__ = ["MuseClient", "get_muse_client"]
