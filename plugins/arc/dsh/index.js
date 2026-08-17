import { readFileSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'

export const name = 'arc-dsh'
export const inject = ['skills']

const adapterDir = dirname(fileURLToPath(import.meta.url))
const skillDir = join(adapterDir, '..', 'skills', 'arc')
const skillPath = join(skillDir, 'SKILL.md')

function readRequiredFrontmatterField(frontmatter, key) {
  const value = new RegExp(`^${key}:\\s*(.+)$`, 'm')
    .exec(frontmatter)?.[1]
    ?.trim()
  if (value === undefined || value === '') {
    throw new Error(`ARC SKILL.md is missing ${key}`)
  }
  return value
}

function readArcSkill() {
  const source = readFileSync(skillPath, 'utf8')
  const match = /^---\r?\n([\s\S]*?)\r?\n---\r?\n([\s\S]*)$/.exec(source)
  if (match === null) {
    throw new Error('ARC SKILL.md is missing YAML frontmatter')
  }

  const skill = {
    name: readRequiredFrontmatterField(match[1], 'name'),
    description: readRequiredFrontmatterField(match[1], 'description'),
    content: match[2],
  }
  if (skill.name !== 'arc') {
    throw new Error(`ARC SKILL.md declares unexpected name: ${skill.name}`)
  }
  return skill
}

export function apply(ctx) {
  ctx.skills.register({
    ...readArcSkill(),
    source: 'bundled',
    path: skillPath,
    resourceBase: { kind: 'directory', path: skillDir },
  })
}
