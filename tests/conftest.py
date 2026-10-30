"""Secrets the application now refuses to start without (config.py); fixed test values, set before any test module imports the app."""
import os

os.environ.setdefault("JWT_SECRET", "test-jwt-secret-0123456789-abcdefghijklmnop")
os.environ.setdefault("ADMIN_SESSION_SECRET", "test-admin-session-secret-0123456789-abcdefgh")
