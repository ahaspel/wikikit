"""The carry lays Wikisource's tables and links on our page, at the place the
two pages' words agree — and keeps ours where ours sets the thing better."""
from wikikit.scan import carry

BEFORE = "The results of the trials made at the works are given in the following table of strengths:\n"
AFTER = "\nThese figures show that the steel ropes were stronger in every case tried by the makers."


def test_a_ruled_table_replaces_our_prose_reading_of_it():
    ws = BEFORE + '{|class="wikitable"\n|Hemp||10 tons\n|-\n|Steel||30 tons\n|}' + AFTER
    ours = BEFORE + "Hemp 10 tons. Steel 30 tons." + AFTER
    out, rep = carry.carry(ours, ws)
    assert '{|class="wikitable"' in out and "Hemp 10 tons." not in out
    assert rep["tables"] == 1 and rep["unplaced"] == []


def test_a_table_that_lays_out_a_formula_leaves_ours():
    ws = BEFORE + "{|\n|sin x||=||x − x³/6\n|}" + AFTER
    ours = BEFORE + "{{block center|<math>\\sin x = x - x^3/6</math>}}" + AFTER
    out, rep = carry.carry(ours, ws)
    assert out == ours
    assert len(rep["tables kept"]["formulas"]) == 1


def test_a_data_table_wins_though_ours_set_its_figures_as_math():
    ws = BEFORE + "{|class=_tablecolhdborder\n|Hemp||1/2 inch\n|-\n|Steel||3/4 inch\n|}" + AFTER
    ours = BEFORE + "{|\n|Hemp||<math>\\tfrac12</math> inch\n|-\n|Steel||<math>\\tfrac34</math> inch\n|}" + AFTER
    out, rep = carry.carry(ours, ws)
    assert "_tablecolhdborder" in out and "<math>" not in out


def test_a_table_replaces_our_table_and_the_caption_we_set_apart():
    head = "Table of Errors in the Prediction"
    ws = BEFORE + "{|\n|+" + head + "\n|-\n|Time||Height\n|-\n|5||10\n|}" + AFTER
    ours = BEFORE + "{{c|''" + head + "''}}\n\n{|\n|Time||Height\n|-\n|5||10\n|}" + AFTER
    out, rep = carry.carry(ours, ws)
    assert out.count(head) == 1 and "|+" + head in out
    assert rep["tables"] == 1


def test_a_table_left_unfinished_on_wikisource_leaves_ours():
    names = ["Diamond", "Graphite", "Sulphur", "Arsenic", "Antimony", "Bismuth",
             "Gold", "Silver", "Copper", "Platinum"]
    def rows(ns):
        return "|-\n".join(f"|{n}||native {n.lower()}\n" for n in ns)
    ws =BEFORE + "{|\n" + rows(names[:6]) + "|-\n|. . .\n|}" + AFTER     # rows left untranscribed
    ours = BEFORE + "{|\n" + rows(names) + "|}" + AFTER
    out, rep = carry.carry(ours, ws)
    assert out == ours
    assert len(rep["tables kept"]["incomplete"]) == 1


def test_a_one_column_box_of_lines_ours_has_is_no_table():
    ws = BEFORE + "{|\n|Fig. 13.—Price-computing Spring Machine.\n|}" + AFTER
    ours = BEFORE + "{{c|{{sc|Fig.}} 13.—Price-computing Spring Machine.}}" + AFTER
    out, rep = carry.carry(ours, ws)
    assert out == ours and len(rep["tables kept"]["boxes"]) == 1


def test_tables_back_to_back_are_placed_as_one():
    ws = BEFORE + "{|\n|1||No. 1||spreading machine\n|}\n{|\n|1||spiral||drawing frame\n|}" + AFTER
    ours = BEFORE + "<poem>1 No. 1 spreading machine\n1 spiral drawing frame</poem>" + AFTER
    out, rep = carry.carry(ours, ws)
    assert "<poem>" not in out and out.count("{|") == 2
    assert rep["unplaced"] == []


def test_a_table_bringing_a_note_without_its_text_is_unplaced():
    ws = BEFORE + "{|class=wikitable\n|Washington<ref name=f1/>||8.6\n|}" + AFTER
    ours = BEFORE + "{|\n|Washington<ref>Interpolated from Hann.</ref>||8.6\n|}" + AFTER
    out, rep = carry.carry(ours, ws)
    assert out == ours and rep["unplaced"]


def test_a_link_goes_on_our_words_inside_our_small_capitals():
    ws = "the measurement of heat (see {{1911link|Thermodynamics}}, § 7) is described"
    ours = "the measurement of heat (see {{sc|Thermodynamics}}, § 7) is described"
    out, rep = carry.carry(ours, ws)
    assert "{{sc|{{1911link|Thermodynamics}}}}" in out and rep["links"] == 1


def test_a_links_display_takes_the_punctuation_ours_has_too():
    ws = "is included in the article Heat, §§ {{EB1911 article link|§ 2|Heat#Meanings}} and three"
    ours = "is included in the article Heat, §§ 2 and three"
    out, _ = carry.carry(ours, ws)
    assert "§§ 2" not in out and "§{{EB1911 article link|§ 2|Heat#Meanings}} and" in out


def test_our_differing_words_become_the_display_and_theirs_the_target():
    ws = "see also {{EB1911 article link|Hesilrige, Sir Arthur}}; and the others named there"
    ours = "see also {{sc|Hesibrige, Sir Arthur}}; and the others named there"
    out, _ = carry.carry(ours, ws)
    assert "{{EB1911 article link|Hesibrige, Sir Arthur|Hesilrige, Sir Arthur}}" in out


def test_a_links_target_and_named_parameters_are_not_its_words():
    assert carry.mask("{{EB1911 article link|nosc=x|onyx|Onyx}}").split() == ["onyx"]


def test_a_table_placed_by_its_words_takes_no_markup_beside_them():
    # the words after the table open our next paragraph's small type, and
    # the words before it sit inside Wikisource's formula template
    ws = "{{center|{{dual line|CH{{sub|3}}·CO|R·CHO}}}}\n{|\n|—CO||C:CH\n|}\nThe diketones of the type yield pyridines."
    ours = "{|\n|CH<sub>3</sub>·CO||R·CHO\n|}\n\n{{smaller block|The diketones of the type yield pyridines.}}"
    out, rep = carry.carry(ours, ws)
    assert "{{smaller block|The diketones" in out and out.count("}}") == out.count("{{")
    assert "|—CO||C:CH" in out and "R·CHO}}}}" not in out
    assert carry.markup_balance(out) == ()


def test_a_links_display_takes_no_close_of_a_template():
    ws = "under {{EB1911 article link|{{small-caps|Pianoforte}}}} is described"
    ours = "under {{sc|Pianoforte}} is described"
    out, _ = carry.carry(ours, ws)
    assert carry.markup_balance(out) == () and out.startswith("under {{sc|")


def test_our_grid_of_a_chemical_formula_stands():
    ws = BEFORE + "{|\n|R·C·H||and||R·C·H\n|-\n|N·OH||||HO·N\n|}" + AFTER
    ours = BEFORE + '{| class="chem-grid"\n|R||·||C||·||H\n|-\n|N·OH||||HO·N\n|}' + AFTER
    out, rep = carry.carry(ours, ws)
    assert out == ours and len(rep["tables kept"]["formulas"]) == 1
