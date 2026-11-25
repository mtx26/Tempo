"""Synthetic coach plans (.docx) with the structure of the coach's real documents.

Structure observed on the real plans (2026-11): a 1x2 table ``S3 | 23 au 29/11/2026``, then a
table whose first row is one merged cell (e.g. the athlete's VMA) and whose other rows are a
label followed by seven cells, Monday..Sunday, without weekday names. The text below is
invented: no real personal data.
"""

import io

from docx import Document

THEME = "Thème de la séance"

# Rows: label, then Monday..Sunday ("" = no training).
EXAMPLE_WEEK = {
    "Objectif": [
        "",
        "",
        "Préparation \nHivernale",
        "",
        "Préparation \nHivernale",
        "",
        "Préparation \nHivernale",
    ],
    THEME: ["Repos", "", "Test VMA", "", "Endurance\nFondamentale", "", "Sortie\nLongue"],
    "Contenu": [
        "",
        "",
        "RAC : 10 \u2018\nTest VMA sur piste",
        "",
        "1h15\u2019\nEn fin de footing 3x80m en déroulant\nr 30\u2019\u2019",
        "",
        "1h25\u2019\nSur parcours vallonné avec accélérations en côtes",
    ],
}


def plan_docx(
    week_range: str = "23 au 29/11/2026",
    week: dict[str, list[str]] | None = None,
    *,
    label: str = "S3",
    banner: str = "VMA 17,4",
) -> bytes:
    rows = week or EXAMPLE_WEEK
    width = 1 + max(len(cells) for cells in rows.values())
    document = Document()
    heading = document.add_table(rows=1, cols=2)
    heading.rows[0].cells[0].text = label
    heading.rows[0].cells[1].text = week_range
    table = document.add_table(rows=1 + len(rows), cols=width)
    merged = table.rows[0].cells[0].merge(table.rows[0].cells[width - 1])
    merged.text = banner
    for row_index, (name, cells) in enumerate(rows.items(), start=1):
        table.rows[row_index].cells[0].text = name
        for column, value in enumerate(cells, start=1):
            table.rows[row_index].cells[column].text = value
    buffer = io.BytesIO()
    document.save(buffer)
    return buffer.getvalue()
