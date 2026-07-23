MCPB_CMD   := npx --yes @anthropic-ai/mcpb
DIST_DIR   := dist
BUNDLE     := $(DIST_DIR)/zendesk-mcp-server.mcpb

.PHONY: mcpb validate pack clean

## mcpb: manifest を検証してから .mcpb バンドルを生成する
mcpb: validate pack

## validate: manifest.json のスキーマ検証
validate:
	$(MCPB_CMD) validate manifest.json

## pack: カレントディレクトリを .mcpb にパッケージング（.mcpbignore で除外）
pack: validate
	mkdir -p $(DIST_DIR)
	$(MCPB_CMD) pack . $(BUNDLE)

## clean: 生成物を削除
clean:
	rm -rf $(DIST_DIR)
