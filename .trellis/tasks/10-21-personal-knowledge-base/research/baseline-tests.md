# Baseline test status (main @ 1498b534, sandbox without Redis/Mongo)

Command: `uv run pytest -q -p no:cacheprovider --import-mode=importlib tests` (default import mode fails collecting duplicate basename test_storage_limits.py)

EXIT 1
11 failed, 2541 passed, 2 skipped, 32 warnings in 176.69s (0:02:56)

Pre-existing failures:
- FAILED tests/agents/test_team_agent_sandbox_support.py::test_team_agent_node_reads_existing_state_messages_for_recommendations
- FAILED tests/api/routes/test_human_wait.py::test_wait_for_response_awaits_cancelled_distributed_wait_cleanup
- FAILED tests/api/test_persona_preset_routes.py::test_list_persona_presets_returns_real_total
- FAILED tests/infra/agent/wecom/test_preferred_agent_resolve.py::test_wecom_preferred_team_submits_team_agent
- FAILED tests/infra/skill/test_loader_prompt.py::test_build_skills_prompt_requires_transfer_before_execution
- FAILED tests/infra/test_runtime_services.py::test_start_runtime_services_starts_all_distributed_listeners
- FAILED tests/infra/test_runtime_services.py::test_stop_runtime_services_skips_memory_services_when_memory_disabled
- FAILED tests/infra/test_runtime_services.py::test_stop_runtime_services_stops_all_distributed_listeners
- FAILED tests/infra/test_user_login_touch_updated_at.py::test_login_refreshes_updated_at_on_success
- FAILED tests/unit/agents/test_team_prompt_builder.py::test_includes_shared_workflow
- FAILED tests/unit/agents/test_team_prompt_builder.py::test_includes_role_instructions_as_system_constraints

Frontend: `pnpm exec tsc --noEmit -p .` exit 0
