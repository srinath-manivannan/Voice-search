"""Knowing what went wrong, without learning anything that should stay here.

Two files. `scrub` decides what may leave the process; `reporting` decides
where it goes. In that order on purpose: the redaction is not a feature of
the Sentry adapter, it is the thing that has to be true before any adapter
exists, including the one somebody adds next year.
"""
