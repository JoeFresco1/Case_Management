"""Forms application package (P07 / FM-036).

The submit-form use case lives in :mod:`app.application.forms.submit_form`.
This package exists so additional form operations (form definitions
list / simulate / retire) can join submit_form without crowding its
file. AGENTS.md §4.R2 caps files at 1000 lines; sub-package split
keeps each module small.
"""
