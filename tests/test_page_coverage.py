"""Coverage regressions for headings, continuous prose and final answer review."""

import asyncio
import json
from threading import Event

import pytest
from test_pages import PageModel, setup, source

from jarvis.application import page_content
from jarvis.domain.types import StreamDelta, StreamTerminal
from jarvis.infrastructure.sqlite_store import SqliteStore
from jarvis.infrastructure.web_fetch import _Article


def lecture():
    sections = [
        (
            "Probability Distributions and Random Variables",
            [
                "Borel sigma-Algebra",
                "Measurable Functions",
                "Probability Mass Function",
            ],
        ),
        (
            "Examples of Discrete Distributions",
            [
                "Uniform Distribution",
                "Bernoulli Distribution",
                "Binomial Distribution",
                "Geometric Distribution",
                "Poisson Distribution",
            ],
        ),
        ("Poisson Limit Theorem", ["Conditions and limit probabilities"]),
        ("Independence of Random Variables", ["Convolution of Discrete Distributions"]),
        (
            "Discrete Random Vectors",
            ["Joint Probability Mass Function", "Joint PMF Tables"],
        ),
        ("Marginal Distributions", ["Marginal probability and independence"]),
        ("Discrete Distributions in ML", ["Logistic Regression", "Poisson Regression"]),
    ]
    return (
        "\n\n".join(
            f"{i} {title}\n\n"
            + "\n\n".join(
                f"{i}.{j} {child}\n\n"
                + (f"{child} has its own definition, assumptions and examples. " * 15)
                for j, child in enumerate(children, 1)
            )
            for i, (title, children) in enumerate(sections, 1)
        )
        + "\n\nplt.legend()\n\nplt.show()"
    )


def test_outline_retains_all_seven_sections_and_their_subtopics():
    topics = page_content.outline(
        lecture()
        + "\n\n# 1. Uniform distribution X = np.arange(1, 7)\n\n# X ~ Pois(lambda1)\n\n# Plot axes[1].hist(sample)"
    )
    assert len(topics) == 7
    assert topics[1]["subtopics"] == [
        "Uniform Distribution",
        "Bernoulli Distribution",
        "Binomial Distribution",
        "Geometric Distribution",
        "Poisson Distribution",
    ]
    assert topics[-1]["subtopics"] == ["Logistic Regression", "Poisson Regression"]
    assert not any("plt" in str(topic) for topic in topics)


def test_extracted_heading_levels_survive_without_numbering():
    parser = _Article()
    parser.feed(
        "<h1>Document title</h1><pre hidden>Ignore this.</pre><h2>Energy</h2><p>Photons become current.</p><h3>Semiconductors</h3><p>Band gaps matter.</p><pre># Bogus heading\n\nplot_data()</pre><h2>Ecology</h2><p>Soil organisms recycle nutrients.</p>"
    )
    topics = page_content.outline(parser.text())
    assert [topic["title"] for topic in topics] == ["Energy", "Ecology"]
    assert topics[0]["subtopics"] == ["Semiconductors"]


def test_definition_anchors_are_scoped_to_their_own_sections():
    text = "1 Vectors\n\n1.1 Definition\n\nA vector contains several variables.\n\n2 Marginals\n\n2.1 Definition\n\nA marginal sums over other variables."
    excerpts = page_content.anchors(text, page_content.outline(text))
    assert "vector contains" in excerpts["T1"]["Definition"]
    assert "marginal sums" in excerpts["T2"]["Definition"]


@pytest.mark.parametrize(
    "text",
    [
        "The first idea concerns solar cells. " * 40
        + "The middle idea concerns soil ecology. " * 40
        + "The final idea concerns bridge engineering. " * 40,
        "- Apples supply nutrients.\n- Trees store carbon.\n- Rivers transport sediment.\n"
        * 30,
        "Name | Value | Unit\nAlpha | 13 | m\nBeta | 27 | s\nGamma | 39 | kg\n" * 30,
    ],
)
def test_coverage_regions_do_not_depend_on_headings(text):
    assert page_content.outline(text) == []
    value = page_content.payload(
        "Explain the entire page", "https://example.com", "P1", text
    )
    regions = value["units"]
    assert len(regions) == 4
    assigned = [identity for region in regions for identity in region["passages"]]
    assert assigned == [passage["id"] for passage in value["DATA"]]
    schema = page_content.schema(value)["properties"]["units"]
    assert schema["required"] == [region["id"] for region in regions]
    assert all(region["passages"] for region in regions)


def test_tail_boundary_is_not_repeated_in_shrinking_overlap_chunks(tmp_path):
    with SqliteStore(tmp_path) as store:

        async def scenario():
            (
                _chat,
                turn,
                _job,
                _backend,
                _model,
                service,
                _runner,
                invocation,
            ) = await setup(store, lecture(), window=4096, output=2048)
            snapshot = store.get_page_snapshot(invocation.id)
            plan = await service.page_analysis._plan(
                snapshot, store.get_user_message(turn.id), 512, Event()
            )
            ends = [end for _, end in plan]
            assert ends == sorted(set(ends))
            assert ends[-1] == len(snapshot.text)
            assert all(
                end > (plan[i - 1][1] if i else 0) for i, (_, end) in enumerate(plan)
            )
            assert len(plan) < 10
            assert all(end - start > 160 for start, end in plan[-2:])

        asyncio.run(scenario())


def test_missing_region_is_rejected_even_when_other_evidence_is_valid(tmp_path):
    with SqliteStore(tmp_path) as store:

        async def scenario():
            (
                chat,
                turn,
                job,
                _backend,
                model,
                _service,
                runner,
                _invocation,
            ) = await setup(store, source())
            original = model.stream_bounded

            def omit(messages, *args):
                for event in original(messages, *args):
                    if isinstance(event, StreamDelta):
                        parsed = json.loads(event.text)
                        parsed["units"].pop(next(reversed(parsed["units"])))
                        yield StreamDelta(json.dumps(parsed))
                    else:
                        yield event

            model.stream_bounded = omit
            paused = await runner.run_job(job.id)
            assert paused.error_code == "page_analysis_failed"
            assert store.page_analysis_metadata(chat.id)[0]["processed"] == 0
            assert store.get_answer(turn.id) is None

        asyncio.run(scenario())


class ReviewedModel(PageModel):
    overview = True
    omit_repair = False
    omit_subtopic = False
    report_missing = True

    def __init__(self):
        super().__init__()
        self.reviews = []
        self.repairs = []

    def stream_bounded(
        self,
        messages,
        cancel,
        instructions,
        output_tokens,
        choices=(),
        response_schema=None,
    ):
        value = json.loads(messages[0].text)
        if value.get("task") == "review":
            self.reviews.append(value)
            yield StreamDelta(
                json.dumps(
                    {
                        "overview": self.overview,
                        "missing": [value["topics"][-1]["id"]]
                        if self.overview and self.report_missing
                        else [],
                    }
                )
            )
            yield StreamTerminal()
        elif value.get("task") == "repair":
            self.repairs.append(value)
            sections = {
                topic["id"]: {
                    "title": topic["title"],
                    **(
                        {
                            "subtopics": {
                                child: child + ": explanation"
                                for child in topic["subtopics"]
                            }
                        }
                        if topic["subtopics"]
                        else {"text": "Explanation of " + topic["title"]}
                    ),
                }
                for topic in value["topics"]
            }
            if self.omit_repair:
                sections.pop(next(reversed(sections)))
            if self.omit_subtopic:
                sections["T2"]["subtopics"].pop("Uniform Distribution")
            yield StreamDelta(json.dumps({"sections": sections}))
            yield StreamTerminal()
        else:
            yield from super().stream_bounded(
                messages, cancel, instructions, output_tokens, choices, response_schema
            )


@pytest.mark.parametrize("report_missing", [True, False])
def test_overview_repairs_missing_topics_once_with_all_seven_sections(
    tmp_path, report_missing
):
    with SqliteStore(tmp_path) as store:

        async def scenario():
            (
                _chat,
                turn,
                job,
                backend,
                _original,
                service,
                runner,
                _invocation,
            ) = await setup(store, lecture(), window=4096, output=2048)
            model = ReviewedModel()
            model.report_missing = report_missing
            service.provider = service.page_analysis.provider = (
                service.page_analysis.tool_provider
            ) = model
            for _ in range(10):
                result = await runner.run_job(job.id)
                if result.status.value == "succeeded":
                    break
                assert result.error_code == "page_paused"
            assert result.status.value == "succeeded"
            answer = store.get_answer(turn.id).text
            assert answer.count("### ") == 7
            assert (
                "Geometric Distribution" in answer
                and "Marginal" in answer
                and "Logistic Regression" in answer
            )
            assert len(model.reviews) == len(model.repairs) == 1
            assert "Uniform Distribution" in model.repairs[0]["source_anchors"]["T2"]
            assert (
                "has its own definition"
                in model.repairs[0]["source_anchors"]["T2"]["Uniform Distribution"]
            )
            assert backend.calls == 1
            assert model.count_output_tokens(answer) <= service.context.output_allowance

        asyncio.run(scenario())


def test_specific_question_does_not_force_an_entire_page_rewrite(tmp_path):
    with SqliteStore(tmp_path) as store:

        async def scenario():
            (
                _chat,
                turn,
                job,
                _backend,
                _original,
                service,
                runner,
                _invocation,
            ) = await setup(store, lecture(), window=4096, output=2048)
            model = ReviewedModel()
            model.overview = False
            service.provider = service.page_analysis.provider = (
                service.page_analysis.tool_provider
            ) = model
            for _ in range(10):
                result = await runner.run_job(job.id)
                if result.status.value == "succeeded":
                    break
                assert result.error_code == "page_paused"
            assert store.get_answer(turn.id).text == "Answer grounded in saved notes."
            assert model.reviews and not model.repairs

        asyncio.run(scenario())


@pytest.mark.parametrize("omission", ["omit_repair", "omit_subtopic"])
def test_incomplete_repair_is_not_committed_as_a_full_answer(tmp_path, omission):
    with SqliteStore(tmp_path) as store:

        async def scenario():
            (
                _chat,
                turn,
                job,
                _backend,
                _original,
                service,
                runner,
                _invocation,
            ) = await setup(store, lecture(), window=4096, output=2048)
            model = ReviewedModel()
            setattr(model, omission, True)
            service.provider = service.page_analysis.provider = (
                service.page_analysis.tool_provider
            ) = model
            result = await runner.run_job(job.id)
            assert (
                result.error_code == "page_analysis_failed"
                and result.status.value == "interrupted"
            )
            assert store.get_answer(turn.id) is None
            assert store.get_draft(turn.id).text == "Answer grounded in saved notes."

        asyncio.run(scenario())
