import csv
import re
from pathlib import Path


def normalize_number(number):
    """Remove formatting so equivalent E.164 phone strings compare equally."""
    return re.sub(r"\D", "", number)


def load_phonebook(path):
    phonebook = {}
    with Path(path).open(newline="", encoding="utf-8-sig") as phonebook_file:
        for row in csv.DictReader(phonebook_file):
            number = normalize_number(row.get("phone", ""))
            name = (row.get("name") or "").strip()
            if number and name:
                phonebook[number] = name
    return phonebook