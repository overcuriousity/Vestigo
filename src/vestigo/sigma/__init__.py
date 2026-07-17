"""Sigma rule runner (W5): offline YAML detection rules over ClickHouse.

Deterministic signature matching, deliberately separate from the statistical
detectors in ``db/anomaly_stats.py``. Rules come from an admin-managed
directory (``Settings.sigma_rules_path``, an offline file drop) and per-case
uploads (Postgres ``sigma_rules``); hits are written as
``Annotation(origin="system", annotation_type="sigma")`` so they surface in
the existing tag/filter UI. See ``docs/ANOMALY_DETECTION.md`` §13.
"""
