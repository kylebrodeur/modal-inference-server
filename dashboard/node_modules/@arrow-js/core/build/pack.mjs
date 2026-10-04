import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { readdir } from 'node:fs/promises'
import { rm } from 'node:fs/promises'
import { execa } from 'execa'
import chalk from 'chalk'

const info = (message) => console.log(chalk.blue(message))
const error = (message) => console.log(chalk.red(message))
const success = (message) => console.log(chalk.green(message))

const __filename = fileURLToPath(import.meta.url)
const __dirname = dirname(__filename)
const rootDir = resolve(__dirname, '../')

async function clean() {
  await rm(`${rootDir}/dist`, { force: true, recursive: true })
}

async function rollupBuild(build) {
  const args = ['rollup', '-c', 'rollup.config.ts', '--configPlugin', 'typescript']
  if (build) args.push('--environment', `BUILD:${build}`)
  await execa('npx', args)
}

async function removeArtifacts() {
  const files = (await readdir(`${rootDir}/dist`))
    .filter(
      (file) =>
        file.endsWith('.d.ts') &&
        !file.startsWith('index.') &&
        !file.startsWith('internal.')
    )
    .map((file) => `${rootDir}/dist/${file}`)
  await Promise.all(files.map((file) => rm(file, { force: true })))
}

;(async () => {
  try {
    await clean()
    info('Rolling up ESM entries')
    await rollupBuild()
    info('Rolling up IIFE')
    await rollupBuild('iife')
    info('Rolling up types')
    await rollupBuild('types')
    info('Rolling up internal types')
    await rollupBuild('internal-types')
    await removeArtifacts()
    success('Build complete')
  } catch (cause) {
    error('A build error occurred')
    console.log(cause)
    process.exitCode = 1
  }
})()
