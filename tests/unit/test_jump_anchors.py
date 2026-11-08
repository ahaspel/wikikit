"""An article's own jumps land on the printed landmark they name: a numbered
paragraph (NUMBER "§ 39"), an equation number (WAVE "(1)"), a run-in subhead
(OILS "Oil Testing") — and a jump a heading already answers is left alone."""
from wikikit.pipeline.stages.elements._jump_anchors import stamp_jump_anchors


def test_a_numbered_paragraph():
    body = ("«P»38. Text.«P»39. «I»Number of classes«/I».—It appears.«P»"
            "as stated in «LN:#39|§ 39«/LN».")
    out = stamp_jump_anchors(body)
    assert "«P»«ANCHOR:39|39»39. «I»Number of classes" in out


def test_a_paragraph_number_printed_twice_gets_no_anchor():
    body = "«P»3. One.«P»3. Again.«P»see «LN:#3|§ 3«/LN»."
    assert stamp_jump_anchors(body) == body


def test_an_equation_number_after_its_formula():
    body = ("it is «CTR»«MATH:y=f(ct-x),«/MATH» . . . . . (3)«/CTR»«P»"
            "as appears from «LN:#3|(3)«/LN».")
    assert "«ANCHOR:3|3»(3)«/CTR»" in stamp_jump_anchors(body)


def test_equation_numbering_restarts_and_the_jump_takes_the_one_before_it():
    first = "«MATH:a«/MATH»«/TD»«TD[style:text-align:right]»(1)«/TD»"
    second = "«CTR»«MATH:b«/MATH» . . (1)«/CTR»"
    body = (first + " solution of «LN:#1|(1)«/LN». Part two. " + second
            + " from «LN:#1|(1)«/LN».")
    out = stamp_jump_anchors(body)
    assert "«ANCHOR:1|1»(1)«/TD»" in out
    assert "«ANCHOR:1-second|1 second»(1)«/CTR»" in out
    assert out.count("«LN:#1|(1)«/LN»") == 1
    assert "«LN:#1 second|(1)«/LN»" in out


def test_a_run_in_subhead():
    body = ("(see «I»«LN:#Oil Testing|Oil Testing«/LN»«/I» below).«P»"
            "«I»Oil Testing«/I».—Reliable methods.")
    assert "«P»«ANCHOR:oil-testing|Oil Testing»«I»Oil Testing«/I».—" in stamp_jump_anchors(body)


def test_a_jump_a_heading_answers_is_left_alone():
    body = ("«SEC:v-history|V. History»«P»«I»History«/I».—Run-in too.«P»"
            "see «LN:#History|History«/LN».")
    assert stamp_jump_anchors(body) == body


def test_a_number_inside_a_figure_name_is_no_answer():
    # the export's substring tier finds "1" inside "Fig. 1"
    body = ("«ANCHOR:fig-1|Fig. 1»{{IMG:a.jpg}} «CTR»«MATH:x«/MATH» . . (1)«/CTR» "
            "of «LN:#1|(1)«/LN».")
    assert "«ANCHOR:1|1»(1)" in stamp_jump_anchors(body)
