MCPB_CMD   := npx --yes @anthropic-ai/mcpb
DIST_DIR   := dist
BUNDLE     := $(DIST_DIR)/zendesk-mcp-server.mcpb
PYTHON     ?= uv run --group dev python

.PHONY: mcpb validate pack test clean

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

## clean: 生成物を削除
clean:
	rm -rf $(DIST_DIR)
