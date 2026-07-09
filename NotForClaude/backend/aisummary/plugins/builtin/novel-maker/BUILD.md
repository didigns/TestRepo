# novel-maker — build (AssemblyScript → WASM)

This plugin's logic is compiled from `src/main.ts` (AssemblyScript) to
`plugin.wasm`. The app ships the prebuilt `plugin.wasm`; you only need this to
rebuild after editing `src/main.ts`.

## Toolchain

```bash
npm i -g assemblyscript            # provides `asc`
npm i assemblyscript-json          # JSON parse/stringify in AS
```

## Build

```bash
asc src/main.ts -o plugin.wasm --optimize --exportRuntime --lib ./node_modules
```

The module exports `alloc/dealloc/on_init/on_event/on_generate_done/memory` and
imports only the mediated host functions (`host_ui_render`, `host_generate`,
`host_storage_save/delete`, `host_export`, `host_preview`, `host_toast/log`) plus
`env.abort`. There is deliberately **no** network/filesystem import — that is what
makes a T2 plugin leak-proof by construction.

## Required patch to `assemblyscript-json`

The published `assemblyscript-json` has a bug in `Str.stringify()`: for control
characters (e.g. newlines inside scene text) it emits a backslash followed by the
raw control byte, which is invalid JSON and aborts the strict parser on round-trip.
Before building, patch `node_modules/assemblyscript-json/assembly/JSON.ts` so
`Str.stringify()` emits proper escapes (`\n`, `\r`, `\t`, `\b`, `\f`, `\uXXXX`):

```ts
stringify(): string {
  let out = "\"";
  for (let i = 0; i < this._str.length; i++) {
    const c = this._str.charCodeAt(i);
    if (c == 0x22) out += "\\\"";
    else if (c == 0x5c) out += "\\\\";
    else if (c == 0x0a) out += "\\n";
    else if (c == 0x0d) out += "\\r";
    else if (c == 0x09) out += "\\t";
    else if (c == 0x08) out += "\\b";
    else if (c == 0x0c) out += "\\f";
    else if (c < 0x20) { const hex = "0123456789abcdef"; out += "\\u00" + hex.charAt((c >> 4) & 0xf) + hex.charAt(c & 0xf); }
    else out += String.fromCharCode(c);
  }
  return out + "\"";
}
```

The shipped `plugin.wasm` was built with this patch applied.
