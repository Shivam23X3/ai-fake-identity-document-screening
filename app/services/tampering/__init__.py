"""Independent document-tampering forensics module (Step 6).

Self-contained sub-package: metadata parsing → ELA → noise → compression →
copy-move → region (photo/stamp) analysis → optional trained model → signal
fusion → risk score + verdict. The backend pipeline reaches it only through
the ``TamperingProvider`` interface in ``app/services/ai_providers.py``;
everything here is usable standalone (see ``engine.run_tampering_analysis``).

Honesty policy: heuristic forensic signals are leads, never proof. The
module only ever emits the four verdicts documented in ``constants.py`` and
routes anything other than a clean pass to human review.
"""
