import { cpSync, mkdirSync } from "node:fs";
mkdirSync("public/vendor/monaco", { recursive: true });
cpSync("node_modules/monaco-editor/min", "public/vendor/monaco", {
  recursive: true,
});
