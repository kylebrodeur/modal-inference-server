import { component, html, onCleanup, pick, reactive } from '..'
import type { Props } from '..'

const data = reactive({ foo: 'bar', value: 123 })

const ValueOnly = component((props: Props<{ value: number }>) =>
  html`${() => props.value}`
)

ValueOnly(data)
ValueOnly(pick(data, 'value'))

const Missing = component((props: Props<{ missing: string }>) =>
  html`${() => props.missing}`
)

// @ts-expect-error data does not provide the requested prop
Missing(data)

// @ts-expect-error missing is not a valid property
ValueOnly(pick(data, 'missing'))

const Static = component(() => html`<div>ok</div>`)

Static()

// @ts-expect-error props are not accepted by a zero-arg component
Static(data)

const WithCleanup = component(() => {
  const dispose = onCleanup(() => {})
  dispose()
  return html`<div>ok</div>`
})

WithCleanup()
