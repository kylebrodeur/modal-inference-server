import { defineConfig, InputOptions, OutputOptions } from 'rollup'
import typescript from '@rollup/plugin-typescript'
import dts from 'rollup-plugin-dts'

const plugins: InputOptions['plugins'] = []

const output: OutputOptions = {
  dir: 'dist',
}

let input: InputOptions['input'] = {
  index: 'src/index.ts',
  internal: 'src/internal.ts',
}

if (process.env.BUILD === 'types') {
  plugins.push(dts())
  output.file = 'dist/index.d.ts'
  delete output.dir
  input = 'dist/index.d.ts'
} else if (process.env.BUILD === 'internal-types') {
  plugins.push(dts())
  output.file = 'dist/internal.d.ts'
  delete output.dir
  input = 'dist/internal.d.ts'
} else if (process.env.BUILD === 'iife') {
  output.sourcemap = true
  output.name = '$arrow'
  output.format = 'iife'
  output.file = 'dist/index.js'
  delete output.dir
  input = 'src/index.ts'
  plugins.push(
    typescript({
      sourceMap: false,
      exclude: ['rollup.config.ts', 'src/__tests__/**'],
    })
  )
} else {
  output.sourcemap = true
  output.entryFileNames = '[name].mjs'
  output.chunkFileNames = 'chunks/[name]-[hash].mjs'
  plugins.push(
    typescript({
      sourceMap: false,
      exclude: ['rollup.config.ts', 'src/__tests__/**'],
    })
  )
}

const config = defineConfig({
  input,
  output,
  plugins,
  treeshake: {
    moduleSideEffects: false,
    propertyReadSideEffects: false,
    tryCatchDeoptimization: false,
  },
})

export default config
