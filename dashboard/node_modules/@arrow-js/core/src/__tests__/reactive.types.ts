import { reactive } from '..'

const source = reactive({
  count: 2,
  multiplier: 3,
})

const total = reactive(() => source.count * source.multiplier)
const totalValue: number = total.value

const data = reactive({
  count: reactive(() => source.count * source.multiplier),
})

const nestedCount: number = data.count
// @ts-expect-error computed values unwrap to their resolved type
const invalidNestedCount: string = data.count

// @ts-expect-error computed values still expose a typed value channel
const invalidTotalValue: string = totalValue
