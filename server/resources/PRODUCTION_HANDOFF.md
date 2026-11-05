# Pons Studio production handoff v2

This is a derived delivery adapter, not a second project ledger. `project.json` remains canonical. For every newly completed production, create `06_GENERATION_SPECS/production_handoff.json` and register it through the existing project artifact/history contract. Never invent rendered sheets or reference assets. Partial chat/planning responses need not manufacture a finished package.

Read the applicable camera-spatial-design, ai-camera-control and storyboarding-video skills. Design shot size, angle, framing, focus, motivated camera movement, revelation, emotional holds and cutting rhythm for the story. A static shot is valid when its purpose is stated. Ordinary 2D narrative planning does not imply verified 3D geometry. Actual scene-specific 3D feasibility still uses the existing spatial gate.

For every scene, provide real storyboard sheet image(s) with all its shots in story order. Do not substitute a shot list or individual unrelated stills. Decode and view the actual sheets; validate coverage and source hashes with the existing skill tools. If a sheet spans scenes, explicitly list its relevant shots under each scene.

For every scene and cut, use canonical `start_s` and `end_s` as global seconds from video start, with half-open intervals [start, end). Keep cuts chronological and contiguous. Derive duration as end-start. Each sheet panel label must contain scene ID, cut ID, global start–end timecode (HH:MM:SS.mmm) and duration; the sheet also shows its covered scene intervals and cut order. Repeated scenes may have disjoint intervals. Existing source-video frame numbers may use a different fps: derive project frame positions from seconds at project fps.

Once timing exists, the backend exposes scene/cut timing and a derived Markdown design through its local API even while images are incomplete. This CLI distribution has no graphical timeline editor. Draft documents identify missing sheets/prompts. For completed v2 deliveries, the service writes `06_GENERATION_SPECS/storyboard_design.md` after independent validation, before archiving. This reserved export is derived from project.json and validated production_handoff.json; update those sources instead of editing the export. The document includes scene/cut intervals, transitions, sheet references, camera direction, first/last-frame prompts, and both cut-local rhythm and equivalent global intervals. Inspect panel time labels alongside the images before delivery.

For every shot, write independently usable first-frame and last-frame image prompts, plus a provider-neutral motion prompt connecting the states. First/last prompts include actual identity/style source content, subject and prop state, angle, shot size, composition, lighting and continuity. They are text prompts, not generated endpoint images. Generate endpoint images or motion footage only when requested and authorized. Camera movement and motion in this handoff are future production intent; do not claim a still animatic implements camera travel or subject motion.

Example structure (fill actual IDs, paths, timings, hashes and scene-specific prose):

```json
{
  "schema_version": 1,
  "project_id": "actual canonical project ID",
  "source_shots_sha256": "actual digest described below",
  "scenes": [{
    "scene_id": "S01",
    "intent": "What the audience should learn/feel in this scene",
    "storyboard_sheets": [{"path":"04_STORYBOARDS/sheets/actual.png","sha256":"actual file hash","shot_ids":["SH01"]}]
  }],
  "shots": [{
    "shot_id":"SH01", "scene_id":"S01", "duration_s":4,
    "intent":"Specific story/emotional function, distinct from a camera term",
    "first_frame_prompt":"Full start-state prompt with authoritative identity/style and visible layout",
    "last_frame_prompt":"Full end-state prompt with preserved identity and the intended end framing/state",
    "motion_prompt":"Scene state -> subject action -> camera -> timed reveal -> settle. Audio remains in edit cues.",
    "control_level":"SOFT_PROMPT",
    "implementation_status":"planned",
    "camera":{
      "shot_size":"CU", "angle":"eye level",
      "composition":"Specific subject placement, attention and readable negative space",
      "movement":"One motivated movement or deliberate locked-off camera",
      "motivation":"Why this movement or stillness serves this beat",
      "rhythm":"Where anticipation, reveal, reaction, hold and cut fall",
      "continuity":"Actual identity, prop state, screen direction and lighting to preserve"
    },
    "timing":[
      {"start_s":0,"end_s":1,"action":"Start-state/anticipation, shot-local seconds"},
      {"start_s":1,"end_s":3,"action":"Main action and motivated camera/reveal"},
      {"start_s":3,"end_s":4,"action":"End framing, reaction/settle and cut motivation"}
    ]
  }]
}
```

Use actual shot durations `end_s-start_s`. Timing phases are contiguous shot-local seconds, sum to that shot, and do not silently replace the global edit timeline. State deliberate identical first/end frames for a held shot; do not add meaningless motion everywhere. Camera terms alone are insufficient: explain story purpose and observable start/end framing. Source/model schemas have not been chosen for the future motion production: keep `control_level=SOFT_PROMPT`, no invented endpoint fields or hard control guarantees.

Calculate the source shot digest from the current canonical shots (after final changes), not the whole ledger:

```python
hashlib.sha256(json.dumps(project['shots'],sort_keys=True,ensure_ascii=False,separators=(',',':')).encode()).hexdigest()
```

The independent service checks schema, scene/shot coverage, real sheet files/hashes, source digest, nonempty per-shot camera/frame prompts, exact shot durations and contiguous timing. Those checks do not prove artistry, future generated motion, geometric feasibility, user acceptance or publication authority. Inspect actual sheets and report those remaining distinctions honestly.
