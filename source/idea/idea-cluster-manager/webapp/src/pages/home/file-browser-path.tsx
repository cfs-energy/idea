import React, {useCallback, useEffect, useMemo, useRef, useState} from "react";
import {Button, FormField, Input} from "@cloudscape-design/components";
import {InputProps} from "@cloudscape-design/components/input/interfaces";
import Utils from "../../common/utils";

/** The current directory, as a trail of clickable ancestors that can be swapped for a text field and
 * typed or pasted into. Paths arrive from job output, from scripts and from colleagues; without a
 * field to paste one into, reaching a deep directory means clicking down every level of it. */

export interface FileBrowserPathSegment {
    name: string
    path: string
}

export interface FileBrowserPathProps {
    /** The directory being listed. Absolute. */
    path: string
    /** An ancestor in the trail was clicked. */
    onNavigate: (path: string) => void
    /** A typed path was submitted. Resolves to null when the directory opened, or to the message to
     * show against the field when it did not. */
    onSubmitPath: (path: string) => Promise<string | null>
}

const ABSOLUTE_PATH_REQUIRED = 'Enter an absolute path, starting with /.'

// The server rejects any path containing '..' before it looks at the filesystem
// (filesystem_helper.check_access). Nothing is resolved here: no '..', no '~', no relative paths.
const NO_PARENT_REFERENCES = 'Paths containing ".." are not accepted. Type the whole path instead.'

/** The path split into everything a user can click, root first. */
export function pathSegments(path: string): FileBrowserPathSegment[] {
    const segments: FileBrowserPathSegment[] = [{name: 'root', path: '/'}]
    let walked = ''
    path.split('/').forEach((token) => {
        if (Utils.isEmpty(token)) {
            return
        }
        walked = `${walked}/${token}`
        segments.push({name: token, path: walked})
    })
    return segments
}

/** What was typed, as a path the listing call can take. Surrounding whitespace, repeated slashes and
 * trailing slashes are tolerated; anything not absolute is refused by name rather than guessed at. */
export function normalizePathInput(value: string): {path: string, error: null} | {path: null, error: string} {
    const trimmed = value.trim()
    if (!trimmed.startsWith('/')) {
        return {path: null, error: ABSOLUTE_PATH_REQUIRED}
    }
    if (trimmed.includes('..')) {
        return {path: null, error: NO_PARENT_REFERENCES}
    }
    const collapsed = trimmed.replace(/\/{2,}/g, '/').replace(/\/+$/, '')
    return {path: collapsed.length === 0 ? '/' : collapsed, error: null}
}

/** Why a typed path did not open, in words rather than an error code. */
export function describeListingFailure(path: string, error: any): string {
    if (error != null && error.errorCode === 'UNAUTHORIZED_ACCESS') {
        // One error code covers "no such directory", "not a directory" and
        // "not yours", so the message must not claim to know which.
        return `Cannot open ${path}. It does not exist, is not a directory, or you do not have access to it.`
    }
    const message = error != null && typeof error.message === 'string' ? error.message.trim() : ''
    return message.length > 0 ? `Cannot open ${path}: ${message}` : `Cannot open ${path}.`
}

/** Show the deep end of a long trail: the current directory is the part a user needs to see. jsdom
 * has no layout, so this is exercised directly rather than through a render. */
export function scrollTrailToEnd(node: {scrollLeft: number, scrollWidth: number}) {
    node.scrollLeft = node.scrollWidth
}

function FileBrowserPath(props: FileBrowserPathProps) {

    const [editing, setEditing] = useState(false)
    const [value, setValue] = useState('')
    const [error, setError] = useState<string | null>(null)
    const inputRef = useRef<InputProps.Ref>(null)
    const trailRef = useRef<HTMLDivElement>(null)
    // Blur cancels, but not the blur that a click somewhere else causes while a
    // submitted path is still in flight.
    const submitting = useRef(false)

    const segments = useMemo(() => pathSegments(props.path), [props.path])

    useEffect(() => {
        if (trailRef.current != null) {
            scrollTrailToEnd(trailRef.current)
        }
    }, [segments, editing])

    useEffect(() => {
        if (editing) {
            inputRef.current?.focus()
            inputRef.current?.select()
        }
    }, [editing])

    const cancel = useCallback(() => {
        setEditing(false)
        setError(null)
    }, [])

    const submit = useCallback(async () => {
        if (submitting.current) {
            return
        }
        const normalized = normalizePathInput(value)
        if (normalized.path == null) {
            setError(normalized.error)
            return
        }
        submitting.current = true
        let failure: string | null
        try {
            failure = await props.onSubmitPath(normalized.path)
        } finally {
            submitting.current = false
        }
        if (failure == null) {
            setEditing(false)
            setError(null)
            return
        }
        // Stay in the field with the failure against it: the path is usually
        // one character wrong, and the listing behind it is still the old one.
        setError(failure)
        inputRef.current?.focus()
    }, [props, value])

    if (editing) {
        return (
            <div className="soca-file-browser-path">
                <FormField errorText={error} stretch={true}>
                    <Input
                        ref={inputRef}
                        value={value}
                        type="text"
                        ariaLabel="Path"
                        placeholder="/path/to/directory"
                        autoComplete={false}
                        spellcheck={false}
                        onChange={(event) => {
                            setValue(event.detail.value)
                            setError(null)
                        }}
                        onKeyDown={(event) => {
                            if (event.detail.key === 'Enter') {
                                event.preventDefault()
                                submit().finally()
                            } else if (event.detail.key === 'Escape') {
                                event.preventDefault()
                                cancel()
                            }
                        }}
                        onBlur={() => {
                            if (!submitting.current) {
                                cancel()
                            }
                        }}
                    />
                </FormField>
            </div>
        )
    }

    return (
        <div className="soca-file-browser-path">
            <div className="soca-file-browser-path-trail" ref={trailRef}>
                <nav aria-label="Current path">
                    {segments.map((segment, index) => (
                        <span key={segment.path} className="soca-file-browser-path-segment">
                            {index > 0 && <span className="soca-file-browser-path-separator">/</span>}
                            <Button variant="inline-link" onClick={() => props.onNavigate(segment.path)}>
                                {segment.name}
                            </Button>
                        </span>
                    ))}
                </nav>
                {/* The empty space beside the trail, which is where a path bar
                    is clicked to type in one. A real button so that it is also
                    reachable by keyboard, with a label that is read but not
                    seen. */}
                <button
                    type="button"
                    title="Edit path"
                    className="soca-file-browser-path-edit"
                    onClick={() => {
                        setValue(props.path)
                        setError(null)
                        setEditing(true)
                    }}
                >
                    <span className="soca-file-browser-path-edit-label">Edit path</span>
                </button>
            </div>
        </div>
    )
}

export default FileBrowserPath
