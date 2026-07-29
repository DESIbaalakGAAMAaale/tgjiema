# TG文件解码器 — Makefile (R76 O12 统一入口)
#
# ════════════════════════════════════════════════════════════════════════
# R76 整改目标(O12):
#   提供单一入口命令,新拉取仓库后只需 Docker 和 Python,
#   不需要任何个人或生产凭证,即可一条命令完整跑通全部功能。
#
# 使用:
#   make secretless-e2e        # 完整 secretless E2E(15 阶段)
#   make clean-artifacts       # 清理 artifacts/secretless-e2e/
#   make validate-yaml         # 验证 workflow YAML 语法
#   make py-compile            # 编译检查所有 Python 脚本
#   make negative-acceptance   # 负向验收(grep 禁止模式)
# ════════════════════════════════════════════════════════════════════════

.PHONY: secretless-e2e clean-artifacts validate-yaml py-compile negative-acceptance help

# 默认目标
help:
	@echo "TG文件解码器 — Makefile (R76 O12)"
	@echo ""
	@echo "可用目标:"
	@echo "  secretless-e2e        完整 secretless E2E(15 阶段,无真实凭证)"
	@echo "  clean-artifacts       清理 artifacts/secretless-e2e/"
	@echo "  validate-yaml         验证 workflow YAML 语法"
	@echo "  py-compile            编译检查所有 Python 脚本"
	@echo "  negative-acceptance   负向验收(grep 禁止模式)"
	@echo ""
	@echo "R76 O12 要求:新拉取仓库后只需 Docker 和 Python,"
	@echo "          不需要任何个人或生产凭证,即可一条命令完整跑通。"

# R76 O12 主入口:完整 secretless E2E
secretless-e2e:
	@echo "============================================================"
	@echo "  R76 O12: Secretless E2E (single entry point)"
	@echo "============================================================"
	python scripts/run_secretless_release_gates.py
	@echo "============================================================"
	@echo "  SECRETLESS FUNCTIONAL GO"
	@echo "============================================================"

# 清理 artifacts
clean-artifacts:
	@echo "Cleaning artifacts/secretless-e2e/..."
	@rm -rf artifacts/secretless-e2e/
	@rm -f deployment-state-*.json
	@echo "✓ Artifacts cleaned"

# 验证 YAML 语法
validate-yaml:
	@echo "Validating workflow YAML syntax..."
	@python -c "import yaml, sys; \
		files = [ \
			'.github/workflows/secretless-contract-e2e.yml', \
			'.github/workflows/release-gates.yml', \
			'.github/workflows/_promote-verified-rc.yml', \
			'docker-compose.yml', \
			'docker-compose.secretless.yml', \
		]; \
		[print(f'  ✓ {f}') for f in files if yaml.safe_load(open(f))]; \
		print('✓ All YAML files parsed successfully')"
	@echo "✓ YAML validation passed"

# Python 编译检查
py-compile:
	@echo "Compiling all Python scripts..."
	@python -m py_compile scripts/run_secretless_release_gates.py
	@python -m py_compile scripts/deployment_state_machine.py
	@python -m py_compile scripts/e2e_update_adapter.py
	@python -m py_compile scripts/scan_production_bypasses.py
	@python -m py_compile scripts/validate_candidate_manifest.py
	@python -m py_compile scripts/compose_runtime_e2e.py
	@python -m py_compile services/restore_nonce_store.py
	@python -m py_compile services/restore_operation_context.py
	@python -m py_compile services/restore_writer.py
	@python -m py_compile services/restore_capability_file.py
	@python -m py_compile services/restore_orchestrator.py
	@python -m py_compile services/sink_adapters/provider_protocol.py
	@python -m py_compile services/sink_adapters/contract_adapter.py
	@python -m py_compile services/sink_adapters/telegram_adapter.py
	@python -m py_compile tests/support/provider_simulator.py
	@python -m py_compile tests/support/deployment_simulator.py
	@echo "✓ All Python scripts compiled successfully"

# 负向验收:验证无禁止模式存在
negative-acceptance:
	@echo "Running negative acceptance checks..."
	@echo "[1] Check e2e_update_adapter.py for private handler calls..."
	@if grep -E '(_dispatch_media|_process_one_pending|process_queue)' scripts/e2e_update_adapter.py; then \
		echo "::error::Forbidden private handler call found"; exit 1; \
	fi
	@echo "[2] Check for _e2e_file_content_b64..."
	@if grep -r '_e2e_file_content_b64' scripts/ tests/integration/ 2>/dev/null; then \
		echo "::error::Forbidden _e2e_file_content_b64 found"; exit 1; \
	fi
	@echo "[3] Check for bot=None..."
	@if grep -E 'bot\s*=\s*None' scripts/e2e_update_adapter.py; then \
		echo "::error::Forbidden bot=None found"; exit 1; \
	fi
	@echo "[4] Check for docker compose exec ... python -c..."
	@if grep -E 'docker\s+compose\s+exec.*python\s+-c' scripts/e2e_update_adapter.py; then \
		echo "::error::Forbidden docker compose exec ... python -c found"; exit 1; \
	fi
	@echo "[5] Check for lightweight tag fallback..."
	@if grep -E 'lightweight.*fallback|warning.*lightweight' .github/workflows/release-gates.yml; then \
		echo "::error::Forbidden lightweight tag fallback found"; exit 1; \
	fi
	@echo "[6] Check scanner UTILITY_CMD_PREFIXES..."
	@if grep -E '"curl"|"git\s+tag\s+-d"|"git\s+tag"' scripts/scan_production_bypasses.py; then \
		echo "::error::Forbidden curl/git tag in UTILITY_CMD_PREFIXES"; exit 1; \
	fi
	@echo "[7] Check scanner _FUNC_LEVEL_EXEMPT_FUNCTIONS..."
	@if grep -E '"_run"|"_compose_cmd"' scripts/scan_production_bypasses.py; then \
		echo "::error::Forbidden _run/_compose_cmd in _FUNC_LEVEL_EXEMPT_FUNCTIONS"; exit 1; \
	fi
	@echo "[8] Check restore_capability_file.py for /tmp nonce..."
	@if grep -E '/tmp/restore_nonce_store|nonce_store_dir' services/restore_capability_file.py; then \
		echo "::error::Forbidden /tmp nonce store found"; exit 1; \
	fi
	@echo "[9] Check restore_writer.py for capability self-comparison..."
	@if grep -E 'operation_id\s*=\s*capability\.get\(|source_sha\s*=\s*capability\.get\(|expected_nonce\s*=\s*capability\.get\(' services/restore_writer.py; then \
		echo "::error::Forbidden capability self-comparison found"; exit 1; \
	fi
	@echo "[10] Check workflows for forbidden secrets references..."
	@if grep -rE 'secrets\.(TEST_|R2_|COCKROACHDB_)' .github/workflows/secretless-contract-e2e.yml; then \
		echo "::error::Forbidden secrets.* reference found"; exit 1; \
	fi
	@echo "✓ All negative acceptance checks passed"
