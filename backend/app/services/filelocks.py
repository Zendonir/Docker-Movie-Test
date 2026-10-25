"""Gemeinsame Sperrliste: Dateien, die der MKV-Optimizer gerade bearbeitet, prüft der Scanner nicht gleichzeitig."""

optimizer_busy: set[int] = set()
