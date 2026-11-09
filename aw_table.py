"""Rendu de tableaux texte pour la sortie console.

Isolé du rapport : les agrégats n'ont pas à connaître leur mise en forme, et le rendu
se teste sans serveur ActivityWatch.
"""

SEPARATOR = None
"""Marqueur de règle horizontale à placer entre deux groupes de lignes."""

_FILL = "─"
_CORNERS = {
    "top": ("┌", "┬", "┐"),
    "middle": ("├", "┼", "┤"),
    "bottom": ("└", "┴", "┘"),
}


def _rule(widths: list[int], kind: str) -> str:
    left, cross, right = _CORNERS[kind]
    # +2 pour l'espace de marge de chaque côté du contenu de la cellule.
    return left + cross.join(_FILL * (width + 2) for width in widths) + right


def _line(cells: list[str], widths: list[int], aligns: str) -> str:
    padded = (
        f"{cell:{align}{width}}" for cell, width, align in zip(cells, widths, aligns)
    )
    return "│ " + " │ ".join(padded) + " │"


def render_table(
    headers: list[str], rows: list[list[str] | None], aligns: str | None = None
) -> list[str]:
    """Rend un tableau encadré, colonnes dimensionnées sur leur contenu le plus large.

    `aligns` donne un caractère de format par colonne ('<' à gauche, '>' à droite) et
    s'applique aussi aux en-têtes, pour qu'un en-tête surplombe bien sa colonne de
    nombres. Une ligne à SEPARATOR devient une règle horizontale.
    """
    aligns = aligns or "<" * len(headers)
    if len(aligns) != len(headers):
        raise ValueError(
            f"{len(aligns)} alignements pour {len(headers)} colonnes : les deux doivent correspondre"
        )

    body = [row for row in rows if row is not SEPARATOR]
    for row in body:
        if len(row) != len(headers):
            raise ValueError(
                f"Ligne de {len(row)} cellules dans un tableau de {len(headers)} colonnes: {row!r}"
            )
    widths = [
        max([len(header)] + [len(row[index]) for row in body])
        for index, header in enumerate(headers)
    ]

    lines = [
        _rule(widths, "top"),
        _line(headers, widths, aligns),
        _rule(widths, "middle"),
    ]
    for row in rows:
        lines.append(
            _rule(widths, "middle") if row is SEPARATOR else _line(row, widths, aligns)
        )
    lines.append(_rule(widths, "bottom"))
    return lines
