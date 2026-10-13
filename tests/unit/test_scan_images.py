"""A supplied page carries the replaced page's own images (`wikikit.scan.images`)."""
from wikikit.scan import images
from wikikit.scan.cut import Item
from wikikit.scan.transcribe import assemble_page


def src(markup, numbers=(), region=None):
    return {"markup": markup, "numbers": list(numbers), "region": region}


def ours(item, numbers=(), rel=None):
    return {"item": item, "numbers": list(numbers), "rel": rel}


def test_source_images_reads_files_and_editor_regions_but_not_glyphs():
    text = ("x [[File:Langle.svg|12px]] y\n[[File:EB1911 Tide - Fig. 1.png|center|300px]]\n"
            "{{Css image crop\n|Image = EB1911 - Volume 21.djvu\n|Page = 534\n|bSize = 820\n"
            "|cWidth = 376\n|cHeight = 99\n|oTop = 453\n|oLeft = 19\n|Location = center}}\n"
            "{{center|{{SC|Fig}}. 18—English Portrait Lens.}}")
    got = images.source_images(text)
    assert [g["numbers"] for g in got] == [[1], [18]]
    assert got[0]["markup"].startswith("[[File:EB1911 Tide")
    assert got[1]["region"][0] == 19 / 820


def test_file_name_numbers_including_a_range():
    got = images.source_images("[[File:EB1911 Tunnel, 7-8.png|center|200px]]")
    assert got[0]["numbers"] == [7, 8]


def test_pair_by_number_one_to_one():
    placed, problems = images.pair([src("A", [7]), src("B", [8])], [ours("c1f1", [7]), ours("c2f1", [8])])
    assert placed == {"c1f1": ["A"], "c2f1": ["B"]} and not problems


def test_one_wiki_file_of_two_printed_figures_covers_both_of_ours():
    # Strength of Materials 33/34: the wiki keeps them apart, we hold one
    # crop each — or here the reverse: one wiki file, two crops of ours
    placed, problems = images.pair([src("AB", [5, 6])], [ours("c1f1", [5]), ours("c2f1", [6])])
    assert placed == {"c1f1": ["AB"], "c2f1": []} and not problems


def test_two_wiki_images_in_one_crop_of_ours():
    # Topaz: our caption names figs 1 and 2 under one crop
    placed, problems = images.pair([src("A", [1]), src("B", [2])], [ours("c1f1", [1, 2])])
    assert placed == {"c1f1": ["A", "B"]} and not problems


def test_a_neighbour_number_follows_into_the_same_crop():
    # Shipbuilding 57-59: one crop, our caption names 57 only
    placed, problems = images.pair([src("A", [57]), src("B", [58]), src("C", [59])], [ours("c0f1", [57])])
    assert placed == {"c0f1": ["A", "B", "C"]} and not problems


def test_editor_region_goes_to_the_figure_holding_its_centre():
    placed, problems = images.pair([src("R", region=[0.6, 0.5, 0.9, 0.6])],
                                   [ours("c1f1", rel=[0.0, 0.1, 0.45, 0.3]), ours("c2f1", rel=[0.5, 0.45, 1.0, 0.7])])
    assert placed == {"c2f1": ["R"]} and not problems


def test_one_figure_of_ours_left_takes_every_image_left():
    # Spiral p. 692: four printed figures, one crop, no caption read
    placed, problems = images.pair([src(m, [n]) for n, m in enumerate("ABCD", 1)], [ours("c1f1")])
    assert placed == {"c1f1": ["A", "B", "C", "D"]} and not problems


def test_a_figure_wikisource_lacks_keeps_our_crop():
    # Tide p. 959: Wikisource has Fig. 8 here, not Fig. 9
    placed, problems = images.pair([src("A", [8])], [ours("c1f1", [8]), ours("c2f1", [9])])
    assert placed == {"c1f1": ["A"]} and not problems


def test_an_image_with_no_place_is_a_problem_never_a_guess():
    placed, problems = images.pair([src("A"), src("B"), src("C")], [ours("c1f1"), ours("c2f1")])
    assert problems


def test_numbers_out_of_order_are_a_problem():
    placed, problems = images.pair([src("A", [2]), src("B", [1])], [ours("c1f1", [1]), ours("c2f1", [2])])
    assert any("order" in p for p in problems)


def test_assembly_sets_the_source_image_in_place_of_our_crop():
    items = [Item("text", (0, 0, 100, 100), 1, "c1s1"), Item("figure", (0, 40, 100, 60), 1, "c1f1"),
             Item("figure", (0, 70, 100, 80), 1, "c1f2")]
    reading = ("⟦10⟧ The tide rises.\n[[FIGURE]]\n{{c|{{sc|Fig.}} 1.}}\n\n⟦50⟧ And falls.\n[[FIGURE]]\n"
               "{{c|{{sc|Fig.}} 2.}}\n\n⟦80⟧ End.")
    out = assemble_page(items, {"c1s1": reading}, {"c1f1": "crop1.png", "c1f2": "crop2.png"}, 100,
                        source_images={"c1f1": ["[[File:EB1911 Tide - Fig. 1.png|center|300px]]"], "c1f2": []})
    assert "[[File:EB1911 Tide - Fig. 1.png|center|300px]]\n{{c|{{sc|Fig.}} 1.}}" in out
    assert "crop1.png" not in out and "crop2.png" not in out
    assert "{{c|{{sc|Fig.}} 2.}}" in out          # our caption stays where printed


def test_a_region_holding_two_of_our_figures_covers_both():
    # vol 21 p. 508: the editor cut figs 18 and 19 as one region
    placed, problems = images.pair([src("R", [18, 19], region=[0.0, 0.4, 0.5, 0.5])],
                                   [ours("c1f1", [18], rel=[0.05, 0.41, 0.25, 0.49]),
                                    ours("c1f2", [19], rel=[0.3, 0.41, 0.45, 0.47])])
    assert placed == {"c1f1": ["R"], "c1f2": []} and not problems


def test_a_region_over_no_figure_of_ours_follows_the_image_before_it():
    # vol 20 p. 227: our cut missed Fig. 80, printed beside Fig. 79
    placed, problems = images.pair([src("A", [79], region=[0.5, 0.45, 0.72, 0.85]),
                                    src("B", [80], region=[0.74, 0.7, 0.97, 0.85])],
                                   [ours("c2f1", [79], rel=[0.49, 0.44, 0.73, 0.86])])
    assert placed == {"c2f1": ["A", "B"]} and not problems


def test_a_file_set_at_a_characters_width_is_a_glyph():
    got = images.source_images("x [[File:FSlash45.png|20px]] y [[File:EB1911 - Tire - Fig 1.png|200px|center]]")
    assert len(got) == 1 and "Tire" in got[0]["markup"]


def test_a_captions_cross_reference_is_not_its_number():
    # vol 20 p. 227: "Fig. 79.—… Recoil Cylinder of Gun shown in fig. 78."
    assert images.caption_numbers(":{{sc|Fig}}. 79.—Vertical Section … shown in fig. 78.") == {79}


def test_a_shifted_region_never_overrides_order():
    # vol 21 p. 508: the DjVu is framed differently; regions sit a figure
    # to the right of ours, and order is what holds
    placed, problems = images.pair(
        [src("A", region=[0.62, 0.7, 0.75, 0.75]), src("B", region=[0.78, 0.7, 0.95, 0.75])],
        [ours("c2f3", rel=[0.5, 0.7, 0.68, 0.75]), ours("c2f4", rel=[0.7, 0.7, 0.9, 0.75])])
    assert placed == {"c2f3": ["A"], "c2f4": ["B"]} and not problems


def test_a_files_caption_a_table_row_below_gives_its_number():
    # vol 28 p. 478: "…Measures, Pound.jpg" names no figure; the caption
    # row under it does
    text = ("|[[Image:Britannica Weights and Measures, Pound.jpg|175px|right]]\n|-{{Ts|ac}}\n"
            "|{{sc|Fig.}} 2.—Imperial Standard Pound, 1844.\n|}")
    assert images.source_images(text)[0]["numbers"] == [2]


def test_order_never_takes_a_figure_whose_number_contradicts():
    # vol 24 p. 738: figs 17-18 belong in our unnumbered crop, not our fig 12
    placed, problems = images.pair([src("A", [16]), src("B", [17]), src("C", [18])],
                                   [ours("c1f1", [12]), ours("c2f2", [16]), ours("c2f3")])
    assert placed == {"c2f2": ["A"], "c2f3": ["B", "C"]} and not problems


def test_order_sliding_off_its_regions_is_a_problem():
    # vol 22 p. 663: each side lacks a formula row the other has
    placed, problems = images.pair(
        [src("A", region=[0.5, 0.4, 0.9, 0.45]), src("B", region=[0.5, 0.6, 0.9, 0.65])],
        [ours("c1f1", rel=[0.0, 0.1, 0.45, 0.2]), ours("c2f1", rel=[0.5, 0.39, 0.9, 0.46])])
    assert problems


def test_a_picture_standing_in_for_a_missing_table_is_no_figure():
    text = ("the following table:\n{{missing table}}\n{{Css image crop\n|Image = EB1911 - Volume 24.djvu\n"
            "|Page = 998\n|bSize = 820\n|cWidth = 389\n|cHeight = 189\n|oTop = 438\n|oLeft = 15}}\n"
            "[[File:EB1911 - Shipbuilding - Fig 28.png|center|400px]]")
    assert [g["numbers"] for g in images.source_images(text)] == [[28]]
