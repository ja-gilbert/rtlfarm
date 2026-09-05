"""Shared pytest configuration.

Hypothesis runs derandomized so a property-test failure in CI is
reproducible on the next run rather than a one-off.
"""

from hypothesis import settings

settings.register_profile("rtlfarm", derandomize=True, max_examples=200)
settings.load_profile("rtlfarm")
