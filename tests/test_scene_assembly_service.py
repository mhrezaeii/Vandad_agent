"""Unit tests for scene subtitle and display-text composition."""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

from app.core.exceptions import ConflictError
from app.schemas.scenario import Scenario
from app.services.scene_assembly_service import (
    SceneAssemblyService,
    build_scene_on_screen_text,
    build_scene_subtitles,
    build_scene_timeline,
)


def _scenario() -> Scenario:
    return Scenario.model_validate(
        {
            "title": "آزمایش",
            "opening_hook": "یک شروع کوتاه",
            "narration": "این روایت اول است. این روایت دوم است.",
            "scenes": [
                {
                    "index": 0,
                    "narration": "این روایت اول است.",
                    "visual_prompt": "Cinematic city b-roll, vertical 9:16",
                    "duration_seconds": 5,
                    "on_screen_text": "عنوان اول",
                    "subtitle_text": "زیرنویس اول",
                },
                {
                    "index": 1,
                    "narration": "این روایت دوم است.",
                    "visual_prompt": "Close-up product b-roll, vertical 9:16",
                    "duration_seconds": 5,
                    "on_screen_text": "عنوان دوم",
                    "subtitle_text": "زیرنویس دوم",
                },
                {
                    "index": 2,
                    "narration": "دعوت به اقدام",
                    "visual_prompt": "Abstract motion graphics, vertical 9:16",
                    "duration_seconds": 5,
                    "on_screen_text": None,
                    "subtitle_text": "دعوت به اقدام",
                },
            ],
            "target_duration_seconds": 15,
        }
    )


def test_scene_on_screen_text_is_a_separate_top_aligned_ass_track():
    ass = build_scene_on_screen_text(_scenario(), width=1080, height=1920)
    assert "Alignment" in ass
    assert ",1,3,1,8," in ass
    assert "عنوان اول" in ass
    assert "عنوان دوم" in ass
    assert "زیرنویس اول" not in ass


def test_scene_subtitles_remain_separate_from_on_screen_text():
    ass = build_scene_subtitles(_scenario(), width=1080, height=1920)
    assert "زیرنویس اول" in ass
    assert "زیرنویس دوم" in ass
    assert "عنوان اول" not in ass


@pytest.mark.asyncio
async def test_assembly_rejects_missing_narration_unless_silent_is_explicit():
    service = SceneAssemblyService(None, None)
    kwargs = {
        "execution_id": uuid.uuid4(),
        "company_id": uuid.uuid4(),
        "project": SimpleNamespace(),
        "brand": SimpleNamespace(),
        "scene_jobs": [SimpleNamespace(scene_index=0, artifact_id=uuid.uuid4())],
    }
    with pytest.raises(ConflictError, match="Persian narration audio is required"):
        await service.assemble(**kwargs)



def test_scene_timeline_accounts_for_real_transition_overlaps():
    scenario = _scenario()
    scenario.scenes[0].transition = "dissolve"
    scenario.scenes[1].transition = "whip"

    offsets, total_duration = build_scene_timeline(scenario)

    assert offsets == [0.0, 4.65, 9.4]
    assert total_duration == 15.0
    subtitles = build_scene_subtitles(scenario)
    assert "0:00:04.65" in subtitles\n    assert "0:00:15.00" in subtitles


@pytest.mark.asyncio
async def test_assembly_builds_ffmpeg_xfade_filters_for_storyboard_transitions(tmp_path):
    scenario = _scenario()
    scenario.scenes[0].transition = "dissolve"
    scenario.scenes[1].transition = "whip"
    service = SceneAssemblyService(None, None)
    captured = {}

    async def fake_run(command, *, cwd, timeout_seconds):
        captured["command"] = command

    service._run = fake_run
    inputs = [tmp_path / f"scene-{index}.mp4" for index in range(3)]
    await service._compose_transitions(
        inputs,
        tmp_path / "joined.mp4",
        scenario,
        timeout_seconds=30,
        cwd=tmp_path,
    )

    command = captured["command"]
    filter_graph = command[command.index("-filter_complex") + 1]
    assert "xfade=transition=dissolve:duration=0.350:offset=4.650" in filter_graph
    assert "xfade=transition=smoothleft:duration=0.250:offset=9.400" in filter_graph
    assert "tpad=stop_mode=clone:stop_duration=0.600" in filter_graph\n    assert command[command.index("-t") + 1] == "15.000"
