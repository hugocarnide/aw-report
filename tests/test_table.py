"""Tests du rendu de tableau.

Le tableau est la sortie que l'utilisateur lit : ce qui compte est l'alignement des
colonnes et la place des règles, pas la façon dont les lignes sont assemblées.
"""

import pytest

from aw_table import SEPARATOR, render_table


class TestRenderTable:
    def test_columns_fit_the_widest_cell(self):
        lines = render_table(["Mesure", "h"], [["Temps actif", "36.11"]])
        assert lines[0] == "┌─────────────┬───────┐"
        assert lines[1] == "│ Mesure      │ h     │"
        assert lines[3] == "│ Temps actif │ 36.11 │"

    def test_header_can_be_the_widest_cell(self):
        lines = render_table(["Journées", "h"], [["1x7.5", "7.5"]])
        assert lines[3] == "│ 1x7.5    │ 7.5 │"

    def test_alignment_applies_to_cells_and_header(self):
        """Les nombres calés à droite alignent la virgule d'une ligne à l'autre."""
        lines = render_table(
            ["Mesure", "h"], [["AFK", "6.00"], ["Non-AFK", "37.50"]], aligns="<>"
        )
        assert lines[1] == "│ Mesure  │     h │"
        assert lines[3] == "│ AFK     │  6.00 │"
        assert lines[4] == "│ Non-AFK │ 37.50 │"

    def test_separator_becomes_a_rule(self):
        lines = render_table(["a"], [["1"], SEPARATOR, ["2"]])
        assert lines == [
            "┌───┐",
            "│ a │",
            "├───┤",
            "│ 1 │",
            "├───┤",
            "│ 2 │",
            "└───┘",
        ]

    def test_empty_table_keeps_a_single_rule_under_the_header(self):
        assert render_table(["a"], []) == ["┌───┐", "│ a │", "├───┤", "└───┘"]

    def test_row_with_the_wrong_number_of_cells_is_rejected(self):
        with pytest.raises(ValueError, match="2 cellules"):
            render_table(["a", "b", "c"], [["1", "2"]])

    def test_alignment_string_must_cover_every_column(self):
        with pytest.raises(ValueError, match="alignements"):
            render_table(["a", "b"], [["1", "2"]], aligns="<")
