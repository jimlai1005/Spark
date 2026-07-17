"""Filet: M2 multi-instance follower layer.

Cross-follower building blocks (registry, tagged notifications, aggregate
reporting) that sit above the per-follower copytrade engine. Each follower
still runs as its own process with its own env-driven config
(``CopySettings.from_env``); this package only holds the pieces that need
to reason about *all* followers at once.
"""
