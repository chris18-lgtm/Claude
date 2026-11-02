#!/usr/bin/env node
// Bridge between csdl2openapi.py and the odata-openapi npm package.
// stdin:  {"options": {...csdl2openapi options}, "indent": 0|4, "sources": ["<csdl text>", ...]}
// stdout: {"results": [{"output": "<openapi json text>", "messages": [...]} | {"error": "..."}]}
// Serialisation happens here, not in Python, so the output is byte-identical
// to the upstream odata-openapi3 CLI.
"use strict";

const { csdl2openapi } = require("odata-openapi");
const { xml2json } = require("odata-csdl");

function convert(text, options, indent) {
  try {
    const csdl = text.startsWith("<") ? xml2json(text) : JSON.parse(text);
    const messages = [];
    const openapi = csdl2openapi(csdl, { ...options, messages });
    return {
      output: JSON.stringify(openapi, null, indent),
      messages: messages.map((m) => (typeof m === "string" ? m : JSON.stringify(m))),
    };
  } catch (e) {
    return { error: e && e.message ? e.message : String(e) };
  }
}

const chunks = [];
process.stdin.on("data", (c) => chunks.push(c));
process.stdin.on("end", () => {
  const { options, indent, sources } = JSON.parse(Buffer.concat(chunks).toString("utf8"));
  const results = sources.map((text) => convert(text, options, indent));
  process.stdout.write(JSON.stringify({ results }));
});
