"""The tests run in their own schema of the Azure SQL database (the connection string comes from backend/.env), never in the real one.
This file is loaded before any test module, so the setting is in place before the application reads its configuration."""
import os

os.environ["SQL_SCHEMA"] = "pivc_test"
os.environ["STORAGE_DIR"] = "./test_storage"
