"""Scans to Wikisource pages, for a book that has no transcription.

A page is cut into reading-order items (`cut`), its figures located by a
vision model and snapped to the ink (`figures`), each text item transcribed
into the standard Wikisource markup the engine's producers already read
(`transcribe`), and proofread by a person against a rendering held to the
engine's own (`vocabulary`).  The model is reached through one client
(`model`); nothing here asks which book it is reading — the book supplies its
notation and conventions as declared data.
"""
