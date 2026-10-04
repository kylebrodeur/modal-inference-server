# @arrow-js/core

![ArrowJS](./arrow-logo.png)

ArrowJS is a tiny, type-safe reactive UI runtime built around JavaScript modules, template literals, and the DOM.

[Docs](https://arrow-js.com) · [API Reference](https://arrow-js.com/api) · [Playground](https://arrow-js.com/play/)

## What this package does

`@arrow-js/core` is the base ArrowJS runtime.

It gives you:

- `reactive()` for state
- `html` tagged template literals for DOM rendering
- `component()` for reusable view functions
- `watch()` for side effects
- `pick()` / `props()` and `nextTick()` helpers

Use this package when you want Arrow without SSR or hydration.

## Install

```sh
pnpm add @arrow-js/core
```

## Example

```ts
import { html, reactive } from '@arrow-js/core'

const state = reactive({ count: 0 })

html`<button @click="${() => state.count++}">
  Clicked ${() => state.count} times
</button>`(document.body)
```
