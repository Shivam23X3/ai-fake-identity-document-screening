"""Independent OCR extraction module (Step 4).

Self-contained sub-package: image preprocessing → OCR engine → text
cleanup → field extraction → confidence → structured JSON. The backend
pipeline reaches it only through the ``OcrProvider`` /
``PreprocessingProvider`` interfaces in ``app/services/ai_providers.py``;
everything here is usable standalone (see ``app.services.ocr.service``).
"""
