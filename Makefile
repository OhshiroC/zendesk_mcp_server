MCPB_CMD   := npx --yes @anthropic-ai/mcpb
DIST_DIR   := dist
BUNDLE     := $(DIST_DIR)/zendesk-mcp-server.mcpb
APP_ZIP    := $(DIST_DIR)/claude-reply-drafts-zendesk-app.zip
PYTHON     ?= uv run --group dev python

.PHONY: mcpb validate pack test test-js zendesk-app clean

## mcpb: manifest を検証してから .mcpb バンドルを生成する
mcpb: validate pack

## validate: manifest.json のスキーマ検証
validate:
	$(MCPB_CMD) validate manifest.json

## pack: カレントディレクトリを .mcpb にパッケージング（.mcpbignore で除外）
pack: validate
	mkdir -p $(DIST_DIR)
	$(MCPB_CMD) pack . $(BUNDLE)

## test: pytest でテストを実行
test:
	$(PYTHON) -m pytest tests/ -q

## test-js: Zendesk アプリの処理を node のテストランナーで実行
test-js:
	node --test tests/js/*.test.js

## zendesk-app: Zendesk アプリ (zendesk_app/) をアップロード用の zip にする
zendesk-app:
	mkdir -p $(DIST_DIR)
	rm -f $(APP_ZIP)
	cd zendesk_app && zip -r -X ../$(APP_ZIP) manifest.json assets translations -x '.*'

## clean: 生成物を削除
clean:
	rm -rf $(DIST_DIR)
