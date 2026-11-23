"""Synthetic coach plans (.docx) reproducing the documented format (docs/TRAINING_EMAILS.md).

No real personal data: built in memory from the structure described by the user, until an
anonymised real document is provided.
"""

import io

from docx import Document

DAYS = ["Lundi", "Mardi", "Mercredi", "Jeudi", "Vendredi", "Samedi", "Dimanche"]

# Rows: objective, theme, detailed content; columns Monday..Sunday ("" = no training).
EXAMPLE_WEEK = {
    "Objectif": ["", "", "Évaluer la VMA", "", "Endurance", "", "Endurance + force"],
    "Séance": [
        "Repos",
        "",
        "Test VMA",
        "",
        "Endurance fondamentale 1h15'",
        "",
        "Sortie longue 1h25'",
    ],
    "Contenu": [
        "",
        "",
        "RAC : 10'\nTest VMA sur piste (protocole demi-Cooper)",
        "",
        'Footing en aisance respiratoire\nEn fin de footing : 3x80m r 30"',
        "",
        "Terrain vallonné\nAccélérations en côte",
    ],
}


def plan_docx(
    title: str = "Semaine du 23 au 29/11/2026",
    week: dict[str, list[str]] | None = None,
    *,
    title_in_table: bool = False,
    days: list[str] | None = None,
) -> bytes:
    document = Document()
    document.add_paragraph("Plan Matis")
    if not title_in_table:
        document.add_paragraph(title)
    rows = week or EXAMPLE_WEEK
    header = days or DAYS
    table = document.add_table(rows=1 + len(rows) + (1 if title_in_table else 0), cols=8)
    offset = 0
    if title_in_table:
        table.rows[0].cells[0].text = title
        offset = 1
    table.rows[offset].cells[0].text = ""
    for index, day in enumerate(header):
        table.rows[offset].cells[index + 1].text = day
    for row_index, (label, cells) in enumerate(rows.items(), start=offset + 1):
        table.rows[row_index].cells[0].text = label
        for column, value in enumerate(cells, start=1):
            table.rows[row_index].cells[column].text = value
    buffer = io.BytesIO()
    document.save(buffer)
    return buffer.getvalue()
