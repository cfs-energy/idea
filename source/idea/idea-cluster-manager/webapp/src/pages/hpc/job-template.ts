import nunjucks from "nunjucks"
import Utils from "../../common/utils"

// A jinja2 job template is written by a scheduler administrator: it is trusted content, rendered in the browser of
// whoever previews or submits it. The rules below are a safeguard that limits what a template can use (a frozen copy
// of the form values read through own data properties, fixed lists of methods, filters, tests and globals, calls only
// to what the rules hand out and to the template's macros, names refused on the parsed template, no loader, bounded
// work). They are not a security boundary against a hostile template author.
const OBJECT_MEMBERS = new Set(Object.getOwnPropertyNames(Object.prototype))
const BLOCKED = new Set(['constructor', 'prototype', '__proto__', '__defineGetter__', '__defineSetter__', '__lookupGetter__', '__lookupSetter__'])
const STRING_METHODS = new Set(['toUpperCase', 'toLowerCase', 'toLocaleUpperCase', 'toLocaleLowerCase', 'trim', 'trimStart',
    'trimEnd', 'trimLeft', 'trimRight', 'split', 'slice', 'substring', 'substr', 'startsWith', 'endsWith', 'includes',
    'indexOf', 'lastIndexOf', 'padStart', 'padEnd', 'repeat', 'charAt', 'charCodeAt', 'codePointAt', 'at', 'concat',
    'replace', 'replaceAll', 'match', 'search', 'localeCompare', 'normalize', 'toString'])
const NUMBER_METHODS = new Set(['toFixed', 'toPrecision', 'toExponential', 'toLocaleString', 'toString'])
const ARRAY_METHODS = new Set(['join', 'slice', 'concat', 'indexOf', 'lastIndexOf', 'includes', 'at', 'flat', 'toString'])
// change the list they are called on: allowed only on lists the template built, never on the frozen values
const ARRAY_MUTATORS = new Set(['push', 'pop', 'shift', 'unshift', 'sort', 'reverse', 'splice'])

// what a template may use besides variables, methods, if, for, set and its own macros
export const TEMPLATE_FILTERS = ['abs', 'batch', 'capitalize', 'center', 'default', 'd', 'dictsort', 'dump', 'escape', 'e',
    'first', 'float', 'groupby', 'indent', 'int', 'join', 'last', 'length', 'list', 'lower', 'nl2br', 'reject', 'rejectattr',
    'replace', 'reverse', 'round', 'safe', 'select', 'selectattr', 'slice', 'sort', 'string', 'striptags', 'sum', 'title',
    'trim', 'truncate', 'upper', 'urlencode', 'wordcount']
export const TEMPLATE_TESTS = ['defined', 'undefined', 'null', 'number', 'string', 'iterable', 'mapping', 'odd',
    'even', 'divisibleby', 'equalto', 'eq', 'sameas', 'ne', 'ge', 'gt', 'greaterthan', 'le', 'lt', 'lessthan', 'lower',
    'upper', 'truthy', 'falsy', 'escaped']
export const TEMPLATE_GLOBALS = ['range', 'joiner']

const runtime = (nunjucks as any).runtime
const SafeString = runtime.SafeString
// the only functions a template may call: what these rules handed out, and its own macros
const CALLABLE = new WeakSet<Function>()
const callable = <F extends Function>(fn: F): F => {CALLABLE.add(fn); return fn}

const primitive = (value: unknown) => value == null || ['string', 'number', 'boolean'].includes(typeof value)
// text, numbers, a template's pattern literal, or a list of those; never a function or another object
const plainArgument = (value: unknown): boolean =>
    primitive(value) || value instanceof SafeString || value instanceof RegExp || (Array.isArray(value) && value.every(plainArgument))

/** No function anywhere in what a filter, test or global hands back. */
function holdsFunction(value: any, depth = 0): boolean {
    if (typeof value === 'function') return true
    if (value == null || typeof value !== 'object' || depth > 4 || value instanceof SafeString || value instanceof RegExp) return false
    return Object.keys(value).some(key => holdsFunction(value[key], depth + 1))
}

/** A method from a fixed list, called on the value itself, with plain arguments, returning a plain value or a list. */
function method(obj: any, prototype: any, name: string): any {
    const fn: Function = prototype[name]
    return callable((...args: any[]) => {
        if (!args.every(plainArgument)) {
            throw new Error(`${name} takes only text, numbers and lists of them in a job template`)
        }
        const result = fn.apply(obj, args.map(arg => arg instanceof SafeString ? String(arg) : arg))
        return primitive(result) || Array.isArray(result) ? result : undefined
    })
}

function memberLookup(obj: any, key: any): any {
    // converted once: the same string is checked and read
    const name = typeof key === 'number' ? String(key) : typeof key === 'string' ? key : undefined
    if (name === undefined || obj == null || typeof obj === 'function' || BLOCKED.has(name)) {
        return undefined
    }
    // a macro's output and safe/escape output are text
    const text = obj instanceof SafeString ? String(obj) : obj
    if (typeof text === 'string') {
        if (name === 'length' || /^\d+$/.test(name)) return text[name as any]
        return STRING_METHODS.has(name) ? method(text, String.prototype, name) : undefined
    }
    if (typeof obj === 'number') {
        return NUMBER_METHODS.has(name) ? method(obj, Number.prototype, name) : undefined
    }
    if (typeof obj !== 'object') {
        return undefined
    }
    if (Array.isArray(obj) && ARRAY_METHODS.has(name)) {
        return method(obj, Array.prototype, name)
    }
    if (Array.isArray(obj) && ARRAY_MUTATORS.has(name)) {
        if (Object.isFrozen(obj)) {
            return callable(() => {throw new Error(`${name} works only on a list the template made: copy it first with slice(0)`)})
        }
        return method(obj, Array.prototype, name)
    }
    // own data properties only: nothing inherited, no accessor, no function
    const descriptor = Object.getOwnPropertyDescriptor(obj, name)
    if (descriptor == null || !('value' in descriptor) || typeof descriptor.value === 'function') {
        return undefined
    }
    return descriptor.value
}

/** A frozen copy of the values: data only, nothing the template can write through, nothing shared. */
function frozenCopy(value: any): any {
    if (Array.isArray(value)) return Object.freeze(value.map(frozenCopy))
    if (value != null && typeof value === 'object') {
        const copy: Record<string, any> = {}
        for (const key of Object.keys(value)) {
            if (!BLOCKED.has(key)) copy[key] = frozenCopy(value[key])
        }
        return Object.freeze(copy)
    }
    return typeof value === 'function' ? undefined : value
}

/** A filter, test or global that takes no function and hands none back. */
function guarded(name: string, fn: Function): Function {
    return callable(function (this: any, ...args: any[]) {
        if (args.some(arg => typeof arg === 'function' || holdsFunction(arg))) {
            throw new Error(`${name} does not take a function in a job template`)
        }
        const result = fn.apply(this, args)
        // a callable these rules handed out (the joiner's) may come back; nothing else that is a function
        if (!(typeof result === 'function' && CALLABLE.has(result)) && holdsFunction(result)) {
            throw new Error(`${name} handed back a function, which a job template may not use`)
        }
        return result
    })
}

/** Only the listed names, with no prototype to inherit from, and nothing anyone can add later. */
function table(source: any, names: string[]): any {
    const result = Object.create(null)
    for (const name of names) {
        if (typeof source[name] !== 'function') throw new Error(`job templates: ${name} is not in this template library`)
        result[name] = guarded(name, source[name])
    }
    return Object.freeze(result)
}

const environment = new nunjucks.Environment([], {autoescape: false}) as any
environment.filters = table(environment.filters, TEMPLATE_FILTERS)
environment.tests = table(environment.tests, TEMPLATE_TESTS)
const joiner = environment.globals.joiner
const range = environment.globals.range
// numbers only, and a bounded count: text arguments loop forever in the library's own range
const MAX_RANGE = 100000
const boundedRange = (...args: any[]) => {
    if (!args.every(arg => arg === undefined || Number.isFinite(arg))) throw new Error('range takes numbers in a job template')
    const [start, stop, step] = args.length === 1 ? [0, args[0], 1] : [args[0], args[1], args[2] ?? 1]
    if (step === 0 || Math.abs((stop - start) / step) > MAX_RANGE) throw new Error(`range gives at most ${MAX_RANGE} numbers in a job template`)
    return range(...args)
}
environment.globals = table({...environment.globals, range: boundedRange, joiner: (sep?: string) => callable(joiner(sep))}, TEMPLATE_GLOBALS)
environment.extensions = Object.freeze(Object.create(null))
environment.asyncFilters = Object.freeze([])

const contextOrFrameLookup = runtime.contextOrFrameLookup
const makeMacro = runtime.makeMacro
const callWrap = runtime.callWrap
runtime.memberLookup = memberLookup
// a bare name otherwise falls through to the members every plain object inherits
runtime.contextOrFrameLookup = (context: any, frame: any, name: string) =>
    OBJECT_MEMBERS.has(name) && !Object.prototype.hasOwnProperty.call(context.ctx, name) ? undefined : contextOrFrameLookup(context, frame, name)
// the template's own macros, and the caller of a call block, may be called
// keyword arguments reach a macro in an object with no prototype, so a parameter the call leaves out is empty
runtime.makeMacro = (...args: any[]) => {
    const macro = makeMacro(...args)
    return callable(function (this: any, ...macroArgs: any[]) {
        const last = macroArgs[macroArgs.length - 1]
        const keywords = Object.create(null)
        if (last != null && typeof last === 'object' && Object.prototype.hasOwnProperty.call(last, '__keywords')) {
            for (const key of Object.keys(last)) keywords[key] = last[key]
            macroArgs[macroArgs.length - 1] = keywords
        } else {
            keywords.__keywords = true
            macroArgs.push(keywords)
        }
        return macro.apply(this, macroArgs)
    })
}
runtime.callWrap = (obj: any, name: string, context: any, args: any[]) => {
    if (typeof obj !== 'function' || !CALLABLE.has(obj)) {
        throw new Error(`Unable to call \`${name}\`, which is undefined or falsey`)
    }
    return callWrap(obj, name, context, args)
}

/** What a template can use by name, for tests and the report. */
export function templateSurface() {
    return {filters: environment.filters, tests: environment.tests, globals: environment.globals, memberLookup, handedOut: (fn: unknown) => typeof fn === 'function' && CALLABLE.has(fn)}
}

export {guarded}

// names the library binds itself on plain objects (macro keywords, dict literals, the render context, loop helpers):
// refused wherever the template declares or uses them
const REFUSED_NAMES = new Set([...OBJECT_MEMBERS, ...BLOCKED, '__keywords'])

/** The template's parsed tree, refused when it declares a name the library would bind with inherited members. */
function refuseBoundNames(template: string) {
    const nodes = (nunjucks as any).nodes
    const visit = (node: any) => {
        if (node == null || typeof node !== 'object') return
        if (Array.isArray(node)) { node.forEach(visit); return }
        if (node instanceof nodes.Symbol && REFUSED_NAMES.has(node.value)) {
            throw new Error(`A job template cannot use the name ${node.value}`)
        }
        if (node instanceof nodes.Pair && node.key instanceof nodes.Literal && REFUSED_NAMES.has(String(node.key.value))) {
            throw new Error(`A job template cannot use the key ${node.key.value}`)
        }
        const targets = node instanceof nodes.For ? [node.name] : node instanceof nodes.Set ? node.targets : []
        for (const target of targets.flatMap((t: any) => t instanceof nodes.Array ? t.children : [t])) {
            if (target instanceof nodes.Symbol && target.value === 'loop') {
                throw new Error('A job template cannot name a variable loop: it is the loop helper')
            }
        }
        for (const field of node.fields ?? []) visit(node[field])
        if (node.children) visit(node.children)
    }
    visit((nunjucks as any).parser.parse(template))
}

// work bounds per render, far above any job script: loop and set steps, and the text written
export const MAX_RENDER_STEPS = 1_000_000
export const MAX_RENDER_OUTPUT = 10_000_000
let budget: {steps: number, output: number} | null = null
const Frame = runtime.Frame
const frameSet = Frame.prototype.set
Frame.prototype.set = function (...args: any[]) {
    if (budget != null && ++budget.steps > MAX_RENDER_STEPS) {
        throw new Error(`This job template takes more than ${MAX_RENDER_STEPS.toLocaleString('en-US')} loop steps; a job script never needs that many. Simplify the loops.`)
    }
    return frameSet.apply(this, args)
}
const suppressValue = runtime.suppressValue
runtime.suppressValue = (...args: any[]) => {
    const text = suppressValue(...args)
    if (budget != null && (budget.output += String(text ?? '').length) > MAX_RENDER_OUTPUT) {
        throw new Error(`This job template writes more than ${MAX_RENDER_OUTPUT.toLocaleString('en-US')} characters; a job script never needs that much.`)
    }
    return text
}

export function renderJobTemplate(template: string, values: Record<string, any>): string {
    refuseBoundNames(template)
    budget = {steps: 0, output: 0}
    try {
        return environment.renderString(template, frozenCopy(values))
    } finally {
        budget = null
    }
}

/**
 * Replaces each %name% of a plain-text template in one pass: a value is written as it is, never read as a
 * replacement pattern or searched again for another placeholder. Only the form's parameter names (and
 * project_name) are placeholders, matched by name, so a run like %Y%m%d_ before one is left as it is.
 */
export function replacePlaceholders(template: string, values: Record<string, any>, names: string[]): string {
    const known = [...new Set(['project_name', ...names])].sort((a, b) => b.length - a.length)
    const pattern = new RegExp(`%(${known.map(name => name.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')).join('|')})%`, 'g')
    return template.replace(pattern, (_match, name) => Utils.isEmpty(values[name]) ? '' : String(values[name]))
}
