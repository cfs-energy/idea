/*
 * Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
 *
 * Licensed under the Apache License, Version 2.0 (the "License"). You may not use this file except in compliance
 * with the License. A copy of the License is located at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 *
 * or in the 'license' file accompanying this file. This file is distributed on an 'AS IS' BASIS, WITHOUT WARRANTIES
 * OR CONDITIONS OF ANY KIND, express or implied. See the License for the specific language governing permissions
 * and limitations under the License.
 */

import React, {Component, RefObject} from "react";

import {FileBrowserClient} from '../../client'
import {AppContext} from "../../common";
import {ListFilesResult} from '../../client/data-model'
import {LocalStorageService} from '../../service'
import Utils from "../../common/utils";
import {registerAceWorkerUrls} from "../../common/ace-worker-urls";
import {Alert, Box, Button, ButtonDropdown, CodeEditor, ColumnLayout, Container, Header, Link, Modal, SpaceBetween, StatusIndicator, Tabs, TextContent, Tiles, Table, Input, FormField} from "@cloudscape-design/components";
import {ButtonDropdownProps} from "@cloudscape-design/components/button-dropdown";
import {toast} from "react-toastify";
import FileBrowserTable, {entryIcon, entryKey, formatFileSize, FileBrowserEntry, FileBrowserMenuItem} from "./file-browser-table";
import DeleteFolderDialog from "./delete-folder-dialog";
import FileBrowserPath, {describeListingFailure} from "./file-browser-path";
import {bastionHost, describeKeyFailure} from "./bastion-host";

import 'ace-builds/css/ace.css';
import 'ace-builds/css/theme/dawn.css';
import 'ace-builds/css/theme/github_light_default.css';
import 'ace-builds/css/theme/github_dark.css';

import {CodeEditorProps} from "@cloudscape-design/components/code-editor";
import {faDownload} from "@fortawesome/free-solid-svg-icons";
import Uppy from "@uppy/core";
import XHRUpload from "@uppy/xhr-upload";
import Dashboard from "@uppy/dashboard";
import '@uppy/core/css/style.css'
import '@uppy/dashboard/css/style.css'
import IdeaForm from "../../components/form";
import {IdeaSideNavigationProps} from "../../components/side-navigation";
import IdeaAppLayout, {IdeaAppLayoutProps} from "../../components/app-layout";
import {FontAwesomeIcon} from "@fortawesome/react-fontawesome";
import {KeyValue} from "../../components/key-value";
import {Constants} from "../../common/constants";
import IdeaConfirm from "../../components/modals";
import {withRouter} from "../../navigation/navigation-utils";

export interface IdeaFileBrowserProps extends IdeaAppLayoutProps, IdeaSideNavigationProps {

}

export interface IdeaFileBrowserState {
    files: FileBrowserEntry[]
    favorites: FileBrowserEntry[]
    cwd: string
    listing: boolean
    selectedFiles: FileBrowserEntry[]
    selectedFavorites: FileBrowserEntry[]
    showHiddenFiles: boolean
    filesToDelete: FileBrowserEntry[]
    showDeleteConfirmModal: boolean
    folderToDelete: {path: string, name: string} | null
    editorOpen: boolean
    fileUploadResult: any
    activeTabId: string
    downloadPemLoading: boolean
    downloadPpkLoading: boolean
    sshHostIp: string
    sshAccess: boolean
    fileTransferMethod: string
    filesToRename: FileBrowserEntry[]
    showRenameModal: boolean
    renameFormValues: {[fileId: string]: string}
    renameValidationErrors: {[fileId: string]: string}
    filePermissions: Map<string, any>
    homeDir: string
    listError: string | null
    listingLoaded?: boolean
    cannotOpen: {file: FileBrowserEntry, reason: 'large' | 'binary'} | null
}

/** One operation on the current selection, offered by the toolbar and the row menu. */
interface FileBrowserAction {
    id: string
    text: string
    enabled: boolean
    run: () => void
}

export interface IdeaFileEditorProps {
    show: boolean
    onSave: (file: string, content: string) => Promise<boolean>
    onClose: () => void
    language?: CodeEditorProps.Language
    filepath?: string
    content?: string
}

export interface IdeaFileEditorState {
    ace: any
    preferences: any
    language: CodeEditorProps.Language
    filepath: string,
    content: string,
    onSaveMessage: React.ReactNode | null
}

class IdeaFileEditorModal extends Component<IdeaFileEditorProps, IdeaFileEditorState> {

    onSaveTimeout: any | null = null

    constructor(props: IdeaFileEditorProps) {
        super(props);
        this.state = {
            ace: undefined,
            preferences: undefined,
            filepath: (this.props.filepath) ? this.props.filepath : '',
            content: (this.props.content) ? this.props.content : '',
            language: (this.props.language) ? this.props.language : 'text',
            onSaveMessage: null
        }
    }

    componentDidMount() {
        import('ace-builds').then(ace => {
            import('ace-builds/esm-resolver').then(() => {
                // esm-resolver does not register worker URLs; see ace-worker-urls.ts
                registerAceWorkerUrls(ace)
                ace.config.set('useStrictCSP', true)
                ace.config.set('loadWorkerFromBlob', false)

                // Detect current mode and set appropriate theme
                const isDarkMode = AppContext.get().isDarkMode();
                const editorTheme = isDarkMode ? 'github_dark' : 'github_light_default';

                this.setState({
                    ace: ace,
                    preferences: {
                        wrapLines: true,
                        theme: editorTheme,
                        showGutter: true,
                        showLineNumbers: true,
                        showInvisibles: false,
                        showPrintMargin: false
                    }
                })
            })
        })
    }

    setContent(filepath: string, content: string) {
        this.setState({
            filepath: filepath,
            content: content
        })
    }

    setLanguage(language: CodeEditorProps.Language) {
        this.setState({
            language: language
        })
    }

    render() {

        const showStatus = (success: boolean, errorMessage?: string) => {
            if (this.onSaveTimeout) {
                clearTimeout(this.onSaveTimeout)
            }
            this.setState({
                onSaveMessage: (success) ?
                    <StatusIndicator type="success">File saved successfully</StatusIndicator> :
                    <StatusIndicator type="error">Failed to save file: {errorMessage}</StatusIndicator>
            }, () => {
                this.onSaveTimeout = setTimeout(() => {
                    this.setState({
                        onSaveMessage: null
                    })
                }, 10000)
            })
        }

        return <Modal
            visible={this.props.show}
            onDismiss={this.props.onClose}
            size="max"
            header={
                <Box variant="small">{this.state.filepath}</Box>
            }
            footer={
                <div>
                    <Box>
                        {this.state.onSaveMessage && this.state.onSaveMessage}
                    </Box>
                    <Box float="right">
                        <SpaceBetween size="xs" direction="horizontal">
                            <Button variant="normal" onClick={this.props.onClose}>Cancel</Button>
                            <Button variant="primary" onClick={() => {
                                this.props.onSave(this.state.filepath, this.state.content).then(status => {
                                    showStatus(true)
                                }).catch(error => {
                                    if (error.errorCode === 'UNAUTHORIZED_ACCESS') {
                                        showStatus(false, 'Permission denied')
                                    } else {
                                        showStatus(false, error.message)
                                    }
                                })
                            }}>Save</Button>
                        </SpaceBetween>
                    </Box>
                </div>
            }
        >
            <CodeEditor
                ace={this.state.ace}
                language={this.state.language}
                value={this.state.content}
                preferences={this.state.preferences}
                onPreferencesChange={e => this.setState({
                    preferences: e.detail
                })}
                onChange={(e) => {
                    this.setState({
                        content: e.detail.value
                    })
                }}
                loading={false}
                themes={{
                    light: [
                        'github_light_default'
                    ],
                    dark: [
                        'github_dark'
                    ]
                }}
                i18nStrings={{
                    loadingState: "Loading code editor",
                    errorState:
                        "There was an error loading the code editor.",
                    errorStateRecovery: "Retry",
                    editorGroupAriaLabel: "Code editor",
                    statusBarGroupAriaLabel: "Status bar",
                    cursorPosition: (row, column) =>
                        `Ln ${row}, Col ${column}`,
                    errorsTab: "Errors",
                    warningsTab: "Warnings",
                    preferencesButtonAriaLabel: "Preferences",
                    paneCloseButtonAriaLabel: "Close",
                    preferencesModalHeader: "Preferences",
                    preferencesModalCancel: "Cancel",
                    preferencesModalConfirm: "Confirm",
                    preferencesModalWrapLines: "Wrap lines",
                    preferencesModalTheme: "Theme",
                    preferencesModalLightThemes: "Light themes",
                    preferencesModalDarkThemes: "Dark themes"
                }}
            />
        </Modal>
    }

}

const FILE_BROWSER_API_PATH = '/cluster-manager/api/v1'
const MAX_NAMES_TO_DELETE_SHOWN = 20

/** Anything about a finished upload that differs from what the person chose. */
export function describeUploadOutcome(payload?: {
    files_renamed?: {original_name: string, final_name: string}[]
    files_skipped?: string[]
}): string | null {
    const renamed = payload?.files_renamed ?? []
    const skipped = payload?.files_skipped ?? []
    const parts: string[] = []
    if (renamed.length > 0) {
        const shown = renamed.slice(0, 3).map((file) => `${file.original_name} as ${file.final_name}`).join(', ')
        const more = renamed.length - 3
        parts.push(`Saved ${shown}${more > 0 ? `, and ${more} more under new names` : ''}.`)
    }
    if (skipped.length > 0) {
        parts.push(`Skipped ${skipped.slice(0, 3).join(', ')}${skipped.length > 3 ? `, and ${skipped.length - 3} more` : ''}: no usable characters in the name.`)
    }
    return parts.length > 0 ? parts.join(' ') : null
}

/** A path for a query string: names can hold & # + and the like, slashes read better as they are. */
export function encodePath(path: string): string {
    return path.split('/').map(encodeURIComponent).join('/')
}

/** Why an upload failed, from the server's answer, without error codes. */
export function describeUploadFailure(responseText?: string): string {
    let answer: {error_code?: string, message?: string} = {}
    try {
        answer = JSON.parse(responseText ?? '{}')
    } catch {
        // an answer from something in front of the server, not worth showing
    }
    if (answer.error_code === 'UNAUTHORIZED_ACCESS') {
        return "You don't have permission to add files to this folder."
    }
    return answer.message ?? 'The upload failed. Try again.'
}

/** Why a download did not start, without error codes. */
export function describeDownloadFailure(what: string, error: {errorCode?: string, message?: string}): string {
    if (error?.errorCode === 'FILE_OPERATION_TIMED_OUT') {
        return `${what} took too long to prepare and was stopped. For large folders, use the commands on the SSH access page.`
    }
    if (error?.errorCode === 'UNAUTHORIZED_ACCESS') {
        return `You do not have access to ${what}. If you need it, ask its owner.`
    }
    return `Could not download ${what}: ${error?.message ?? 'unknown error'}`
}

// atob and btoa work on bytes as characters, so µ arrives as Âµ and → cannot be sent at all.
export function decodeFileContent(base64: string): string {
    return new TextDecoder().decode(Uint8Array.from(atob(base64), (char) => char.charCodeAt(0)))
}

export function encodeFileContent(content: string): string {
    return btoa(Array.from(new TextEncoder().encode(content), (byte) => String.fromCharCode(byte)).join(''))
}

class IdeaFileBrowser extends Component<IdeaFileBrowserProps, IdeaFileBrowserState> {

    fileEditor: RefObject<IdeaFileEditorModal | null>
    _fileBrowserClient: FileBrowserClient
    createFolderForm: RefObject<IdeaForm | null>
    deleteFileConfirmModal: RefObject<IdeaConfirm | null>
    localStorage: LocalStorageService


    constructor(props: IdeaFileBrowserProps) {
        super(props);

        const localStoragePrefix = (): string => {
            const authService = AppContext.get().auth()
            return `${authService.getAwsRegion()}/${authService.getClusterName()}/${authService.getUsername()}`
        }

        this.fileEditor = React.createRef()
        this.localStorage = new LocalStorageService({
            prefix: localStoragePrefix()
        })
        this.state = {
            files: [],
            favorites: [],
            cwd: '/',
            listing: true,
            selectedFiles: [],
            selectedFavorites: [],
            showHiddenFiles: false,
            editorOpen: false,
            fileUploadResult: null,
            activeTabId: 'files',
            downloadPemLoading: false,
            downloadPpkLoading: false,
            sshHostIp: '',
            sshAccess: false,
            fileTransferMethod: 'file-zilla',
            filesToDelete: [],
            showDeleteConfirmModal: false,
            folderToDelete: null,
            filesToRename: [],
            showRenameModal: false,
            renameFormValues: {},
            renameValidationErrors: {},
            filePermissions: new Map<string, any>(),
            homeDir: '',
            listError: null,
            cannotOpen: null
        }
        this._fileBrowserClient = AppContext.get().client().fileBrowser()
        this.createFolderForm = React.createRef()
        this.deleteFileConfirmModal = React.createRef()
    }

    getCreateFolderForm(): IdeaForm {
        return this.createFolderForm.current!
    }



    componentDidMount() {
        AppContext.get().getClusterSettingsService().getModuleSettings(Constants.MODULE_BASTION_HOST).then(moduleInfo => {
            this.setState({
                sshHostIp: bastionHost(moduleInfo),
                sshAccess: true
            })
        }).catch(error => {
            if (error.errorCode === 'MODULE_NOT_FOUND') {
                this.setState({
                    sshAccess: false
                })
            }
        })
        // Only labels the trail; without it the trail starts at root as before.
        AppContext.get().auth().getUser().then(user => {
            this.setState({homeDir: Utils.asString(user?.home_dir)})
        }).catch(() => undefined)
        this.listFavorites()
        const cwd = this.props.searchParams.get('cwd')
        this.listFiles((cwd) ? cwd : undefined).finally()
    }

    fileBrowserClient(): FileBrowserClient {
        return this._fileBrowserClient
    }

    convert(payload?: ListFilesResult): FileBrowserEntry[] {
        if (payload?.listing == null) {
            return []
        }
        const files: any = []
        payload.listing.forEach((entry) => {
            files.push({
                id: entry.file_id,
                name: entry.name,
                isDir: entry.is_dir,
                isHidden: entry.is_hidden,
                modDate: entry.mod_date,
                size: entry.size
            })
        })
        return files
    }

    getCwd(): string {
        return this.state.cwd
    }

    checkFileSize(file: FileBrowserEntry): boolean {
        const maxFileSize = 5 * 1024 * 1024; // 5MB in bytes
        if (file.size && file.size > maxFileSize) {
            this.setState({cannotOpen: {file: file, reason: 'large'}});
            return false;
        }
        return true;
    }

    showToast(id: string, message: string, type: 'warning' | 'info' = 'warning') {
        if (type === 'warning') {
            toast.warn(message, {
                // a failure stays until it is dismissed: nobody reads a reason in two seconds
                autoClose: false,
                toastId: id
            })
        } else if (type === 'info') {
            toast.info(message, {
                autoClose: 2000,
                toastId: id
            })
        }
    }

    /** List a directory. `onError` takes the failure instead of the alert above the table, for
     * callers that show it somewhere the user is already looking. */
    listFiles(cwd?: string, onError?: (error: any) => void): Promise<boolean> {
        this.setState({listing: true})
        return this.fileBrowserClient().listFiles({
            cwd: cwd
        }).then((result: ListFilesResult) => {
            this.props.searchParams.set('cwd', result.cwd!)
            this.props.setSearchParams(this.props.searchParams)
            this.setState({
                files: this.convert(result),
                cwd: Utils.asString(result.cwd, '/'),
                selectedFiles: [],
                listing: false,
                listError: null,
                listingLoaded: true
            })
            return true
        }).catch(error => {
            // the listing the user is looking at stays put; only the failure is new
            this.setState({listing: false})
            if (onError != null) {
                onError(error)
            } else {
                this.setState({listError: describeListingFailure(cwd ?? 'your home folder', error)})
            }
            return false
        })
    }

    /** Open a path the user typed. Resolves to null when it opened, or to the message the path bar
     * should show when it did not. */
    navigateToTypedPath(path: string): Promise<string | null> {
        const failure: {message: string | null} = {message: null}
        return this.listFiles(path, (error) => {
            failure.message = describeListingFailure(path, error)
        }).then((listed) => (listed ? null : (failure.message ?? `Cannot open ${path}.`)))
    }

    listFavorites() {
        const favorites = this.localStorage.getItem('favorites')
        if (favorites == null) {
            this.setState({
                favorites: []
            })
            return
        }
        const files = JSON.parse(favorites)
        this.setState({
            favorites: files
        })
    }

    addFavorite(file: FileBrowserEntry) {
        const favorites = [...this.state.favorites]
        const path = `${this.getCwd()}/${file.name}`
        const parent = path.substring(0, path.lastIndexOf('/'))
        const favorite = {
            id: file.id,
            name: `(${parent}) ${file.name}`,
            isDir: file.isDir,
            size: file.size,
            modDate: file.modDate,
            path: path
        }
        const found = favorites.find((file) => file.path === path)
        if (found == null) {
            favorites.push(favorite)
            this.localStorage.setItem('favorites', JSON.stringify(favorites))
            this.setState({
                favorites: favorites
            })
        }
    }

    removeFavorite(file: FileBrowserEntry) {
        const favorites = [...this.state.favorites]
        for (let i = 0; i < favorites.length; i++) {
            let favorite = favorites[i]
            if (favorite.path === file.path) {
                favorites.splice(i, 1)
                this.localStorage.setItem('favorites', JSON.stringify(favorites))
                this.setState({
                    favorites: favorites
                })
                break
            }
        }
    }

    getFilePath(file: FileBrowserEntry): string {
        let path = file.path
        if (path == null) {
            let cwd = this.getCwd()
            if (cwd === '/') {
                path = '/' + file.name
            } else {
                path = cwd + '/' + file.name
            }
        }
        return path
    }

    /** Ask for a signed link and hand it to the browser. Rejects when there is nothing to hand over,
     * because the server answers a refused file with a 200 and no link. */
    startBrowserDownload(file: string): Promise<void> {
        const fileName = file.substring(file.lastIndexOf('/') + 1)
        return AppContext.get().auth().getAccessToken().then(accessToken => {
            return fetch(`${AppContext.get().client().fileBrowser().getEndpointUrl()}/generate-download-url`, {
                method: 'POST',
                headers: {
                    'Authorization': `Bearer ${accessToken}`,
                    'Content-Type': 'application/json'
                },
                body: JSON.stringify({ file: file })
            })
        }).then(response => {
            if (!response.ok) {
                throw {message: `the server answered ${response.status}`}
            }
            return response.json()
        }).then(result => {
            if (result.success === false || !result.download_url) {
                throw {errorCode: result.error_code, message: result.message}
            }
            const link = document.createElement('a')
            link.href = result.download_url
            link.setAttribute('download', fileName)
            link.style.display = 'none'
            document.body.appendChild(link)
            link.click()
            document.body.removeChild(link)
        })
    }

    /** Swap one flashbar item for another, or add it when the first has gone. */
    replaceFlash(id: string, item: any) {
        const items = this.props.flashbarItems || []
        const replaced = items.map(current => (current as any).customId === id ? item : current)
        if (!replaced.some(current => (current as any).customId === id)) {
            replaced.push(item)
        }
        this.props.onFlashbarChange({items: replaced})
    }

    removeFlashAfter(id: string, ms: number) {
        setTimeout(() => {
            this.props.onFlashbarChange({
                items: (this.props.flashbarItems || []).filter(item => (item as any).customId !== id)
            })
        }, ms)
    }

    downloadFiles(files: FileBrowserEntry[]) {
        const what = files.length === 1 ? files[0].name : `these ${files.length} files`

        // Determine if we need to show flashbar (for directories, multiple files, or large single files)
        const needsFlashbar = files.length > 1 ||
                             files.some(file => file.isDir) ||
                             files.some(file => file.size && file.size > 10 * 1024 * 1024) // 10MB threshold

        if (files.length === 1 && !needsFlashbar) {
            this.startBrowserDownload(this.getFilePath(files[0])).catch(error => {
                this.showToast('download-error', describeDownloadFailure(what, error))
            })
            return
        }

        // Show loading feedback for downloads that might take time
        const downloadId = `download-${Date.now()}-${Math.random().toString(36).substr(2, 9)}`

        let contentMessage = ''
        if (files.length === 1 && files[0].isDir) {
            contentMessage = `Preparing download archive for directory "${files[0].name}"... This may take several minutes for directories with large files.`
        } else if (files.length === 1) {
            contentMessage = `Preparing download for large file "${files[0].name}"...`
        } else {
            contentMessage = `Preparing download archive for ${files.length} files... This may take several minutes for large files.`
        }

        this.props.onFlashbarChange({
            items: [...(this.props.flashbarItems || []), {
                type: 'info' as const,
                loading: true,
                header: 'Preparing download',
                content: contentMessage,
                dismissible: false,
                customId: downloadId
            } as any]
        })

        const isSingleFile = files.length === 1 && !files[0].isDir
        const prepared: Promise<number> = isSingleFile
            ? this.startBrowserDownload(this.getFilePath(files[0])).then(() => 0)
            : this.fileBrowserClient().downloadFiles({
                files: files.map((file) => this.getFilePath(file))
            }).then(result => this.startBrowserDownload(result.download_url!).then(() => result.skipped_files ?? 0))

        prepared.then((skipped) => {
            if (skipped > 0) {
                // stays until dismissed: the zip is not everything that was asked for
                this.replaceFlash(downloadId, {
                    type: 'warning' as const,
                    header: 'Download ready',
                    content: `Your zip is ready. ${skipped.toLocaleString()} ${skipped === 1 ? 'file' : 'files'} could not be read and ${skipped === 1 ? 'is' : 'are'} not in it.`,
                    dismissible: true,
                    customId: downloadId
                })
                return
            }
            this.replaceFlash(downloadId, {
                type: 'success' as const,
                header: 'Download ready',
                content: 'Your download should start automatically.',
                dismissible: true,
                customId: downloadId
            })
            this.removeFlashAfter(downloadId, isSingleFile ? 5000 : 10000)
        }).catch(error => {
            this.replaceFlash(downloadId, {
                type: 'error' as const,
                header: 'Download failed',
                content: describeDownloadFailure(what, error),
                dismissible: true,
                customId: downloadId
            })
        })
    }

    onOpenSelection(payload?: FileBrowserEntry) {
        if (payload == null) {
            return
        }
        if (payload.isDir) {
            this.listFiles(this.getFilePath(payload)).finally()
        } else {
            this.openFile(payload)
        }
    }

    onOpenFavorite(payload?: FileBrowserEntry) {
        if (payload == null) {
            return
        }
        if (payload.isDir) {
            this.listFiles(payload.path).then((listed) => {
                if (listed) {
                    this.setState({
                        activeTabId: 'files'
                    })
                }
            })
        } else {
            this.openFile(payload)
        }
    }

    buildFileEditor() {
        return (
            <IdeaFileEditorModal
                ref={this.fileEditor}
                show={this.state.editorOpen}
                onSave={(file: string, content: string) => {
                    return this.fileBrowserClient().saveFile({
                        file: file,
                        content: encodeFileContent(content)
                    }).then(() => {
                        return true
                    })
                }}
                onClose={() => {
                    this.setState({
                        editorOpen: false
                    })
                }}
            />
        )
    }

    openFile(file: FileBrowserEntry) {
        // Check file size before attempting to open
        if (!this.checkFileSize(file)) {
            return;
        }

        const path = this.getFilePath(file)
        this.fileBrowserClient().readFile({
            file: path
        }).then(result => {
            this.fileEditor.current?.setContent(path, decodeFileContent(result.content!))
            this.fileEditor.current?.setLanguage('sh')
            this.setState({
                editorOpen: true
            })
        }).catch(error => {
            if (error.errorCode === 'NOT_A_TEXT_FILE') {
                this.setState({cannotOpen: {file: file, reason: 'binary'}})
            } else if (error.errorCode === 'UNAUTHORIZED_ACCESS') {
                this.showToast('open-denied', `You do not have access to ${file.name}. If you need it, ask its owner.`)
            } else {
                this.showToast(error.errorCode, `Could not open ${file.name}: ${error.message}`)
            }
        })
    }

    openFileInScriptWorkbench(file: FileBrowserEntry) {
        // Check file size before attempting to open
        if (!this.checkFileSize(file)) {
            return;
        }

        const path = this.getFilePath(file)
        // First validate that the file is a text file to avoid crashing the server
        this.fileBrowserClient().readFile({
            file: path
        }).then(result => {
            // If successful, the file is a text file and safe to open in script workbench
            this.props.navigate(`/home/script-workbench?file=${encodePath(path)}`)
        }).catch(error => {
            if (error.errorCode === 'NOT_A_TEXT_FILE') {
                this.showToast('binary-file-error', 'Cannot open binary files in Script Workbench. Only text files are supported.')
            } else {
                this.showToast(error.errorCode, `Failed to open file: ${error.message}`)
            }
        })
    }

    showUploadModal() {
        AppContext.get().auth().getAccessToken().then(accessToken => {
            const httpEndpoint = AppContext.get().getHttpEndpoint()
            const uppy = new Uppy()
                .use(Dashboard, {
                    animateOpenClose: false,
                    closeModalOnClickOutside: false,
                    browserBackButtonClose: true,
                    proudlyDisplayPoweredByUppy: false,
                    fileManagerSelectionType: 'both',
                    theme: (AppContext.get().isDarkMode() ? 'dark' : 'light')
                })
                .use(XHRUpload, {
                    endpoint: `${httpEndpoint}${FILE_BROWSER_API_PATH}/upload?cwd=${encodePath(this.getCwd())}`,
                    headers: {
                        'Authorization': `Bearer ${accessToken}`
                    },
                    formData: true,
                    fieldName: 'files[]',
                    method: 'PUT',
                    bundle: true
                })
            const dashboard = uppy.getPlugin('Dashboard')!
            dashboard.openModal()
            uppy.on('upload-error', (_file, _error, response) => {
                uppy.info(describeUploadFailure((response as any)?.responseText), 'error', 15000)
            })
            let outcome: any = null
            uppy.on('upload-success', (_file, response) => {
                // a bundled upload answers once for all its files
                outcome = (response?.body as any)?.payload ?? outcome
            })
            uppy.on('complete', () => {
                const changed = describeUploadOutcome(outcome)
                if (changed != null) {
                    uppy.info(changed, 'warning', 15000)
                }
                this.listFiles(this.getCwd()).finally()
            })
        })
    }

    buildCreateFolderForm() {
        return (
            <IdeaForm ref={this.createFolderForm}
                      name="create-folder"
                      modal={true}
                      modalSize="medium"
                      title="Create folder"
                      onSubmit={() => {
                          if (!this.getCreateFolderForm().validate()) {
                              return
                          }
                          const values = this.getCreateFolderForm().getValues()
                          const invalidName = this.validateFileName(values.name)
                          if (invalidName != null) {
                              this.getCreateFolderForm().setError('INVALID_PARAMS', invalidName)
                              return
                          }
                          this.fileBrowserClient().createFile({
                              cwd: this.getCwd(),
                              filename: values.name,
                              is_folder: true
                          }).then(() => {
                              this.listFiles(this.getCwd()).finally()
                              this.getCreateFolderForm().hideModal()
                          }).catch(error => {
                              this.getCreateFolderForm().setError(error.errorCode, `${error.message}`.replace(/^Invalid params: /, ''))
                          })
                      }}
                      params={[
                          {
                              name: 'name',
                              title: 'Folder name',
                              description: 'Letters, numbers, and _ . - ( ) only. No spaces.',
                              data_type: 'str',
                              param_type: 'text',
                              validate: {
                                  required: true
                              }
                          }
                      ]}
            />
        )
    }

    validateFileName(fileName: string): string | null {
        if (!fileName || fileName.trim() === '') {
            return 'File name cannot be empty'
        }

        // Apply the same validation logic as Utils.to_secure_filename()
        // Normalize unicode characters (simplified version of Python's NFKD)
        let normalizedName = fileName.normalize('NFKD')

        // Check for path separators (like Python's os.path.sep and os.path.altsep)
        if (normalizedName.includes('/') || normalizedName.includes('\\')) {
            return 'File name cannot contain path separators (/ or \\)'
        }

        // Apply the same regex pattern as FILENAME_ASCII_STRIP_RE: [^A-Za-z0-9_.\-()]
        // This allows only letters, numbers, underscore, period, hyphen, and parentheses
        const invalidCharsRegex = /[^A-Za-z0-9_.\-()]/
        if (invalidCharsRegex.test(normalizedName)) {
            return 'File name can only contain letters, numbers, underscore (_), period (.), hyphen (-), and parentheses ()'
        }

        // Check if the filename would be empty after ASCII conversion and processing
        // (similar to how Utils.to_secure_filename returns None for empty results)
        // eslint-disable-next-line no-control-regex
        const asciiOnly = normalizedName.replace(/[^\x00-\x7F]/g, '') // Remove non-ASCII
        if (!asciiOnly.trim()) {
            return 'File name must contain valid ASCII characters'
        }

        // Check for names that are just dots or underscores (similar to strip('._'))
        if (/^[._]+$/.test(normalizedName.trim())) {
            return 'File name cannot be only dots and underscores'
        }

        // Check length (filesystem limit)
        if (fileName.length > 255) {
            return 'File name cannot be longer than 255 characters'
        }

        return null
    }

    buildRenameFileModal() {
        const filesToRename = this.state.filesToRename
        const renameValues = this.state.renameFormValues
        const validationErrors = this.state.renameValidationErrors

        const performRename = async (item: FileBrowserEntry, newName: string) => {
            const itemPath = this.getFilePath(item)
            return this.fileBrowserClient().renameFile({
                file: itemPath,
                new_name: newName
            })
        }

        const handleBulkRename = async () => {
            let successCount = 0
            let errorCount = 0
            const errors: string[] = []

            // Use existing permission data from component state (no need to call API again)
            const permissionMap = this.state.filePermissions

            // Validate inputs (only for items with permission)
            for (const item of filesToRename) {
                const filePath = this.getFilePath(item)
                const permissionInfo = permissionMap.get(filePath)

                // Skip validation for protected files - they'll be skipped during processing
                if (permissionInfo?.is_protected || !permissionInfo?.has_permission) {
                    continue
                }

                const validationError = validationErrors[item.id]

                if (validationError) {
                    errors.push(`${item.name}: ${validationError}`)
                    errorCount++
                }
            }

            if (errors.length > 0) {
                this.showToast('rename-validation-error', errors.join(', '))
                return
            }

            let skippedCount = 0

            // Process each item
            for (const item of filesToRename) {
                try {
                    const filePath = this.getFilePath(item)
                    const permissionInfo = permissionMap.get(filePath)

                    // Skip files without permission or protected files
                    if (permissionInfo?.is_protected || !permissionInfo?.has_permission) {
                        skippedCount++
                        continue
                    }

                    const newName = renameValues[item.id]?.trim()
                    if (newName && newName !== item.name) {
                        await performRename(item, newName)
                        successCount++
                    }
                } catch (error: any) {
                    errorCount++
                    this.showToast(`rename-error-${item.id}`, `Failed to rename ${item.name}: ${error.message}`)
                }
            }

            // Show summary and refresh
            if (successCount > 0 || skippedCount > 0) {
                let message = ''
                if (successCount > 0) {
                    message += `Successfully renamed ${successCount} item(s)`
                }
                if (skippedCount > 0) {
                    if (message) message += '. '
                    message += `${skippedCount} protected or inaccessible item(s) were skipped`
                }
                this.showToast('rename-success', message, 'info')
                this.listFiles(this.getCwd()).finally()
            }

            if (errorCount === 0) {
                this.closeRenameModal()
            }
        }

        const closeRenameModal = () => {
            this.setState({
                filesToRename: [],
                showRenameModal: false,
                renameFormValues: {},
                renameValidationErrors: {}
            })
        }

        const updateRenameValue = (fileId: string, value: string) => {
            const validationError = this.validateFileName(value)
            this.setState({
                renameFormValues: {
                    ...this.state.renameFormValues,
                    [fileId]: value
                },
                renameValidationErrors: {
                    ...this.state.renameValidationErrors,
                    [fileId]: validationError || ''
                }
            })
        }

        // Initialize form values when files are first loaded
        if (filesToRename.length > 0 && Object.keys(renameValues).length === 0) {
            const initialValues: {[fileId: string]: string} = {}
            const initialErrors: {[fileId: string]: string} = {}
            filesToRename.forEach(file => {
                initialValues[file.id] = file.name
                // Initialize with no errors for current names
                initialErrors[file.id] = ''
            })
            this.setState({
                renameFormValues: initialValues,
                renameValidationErrors: initialErrors
            })
        }

        // Use the filePermissions from component state
        const filePermissions = this.state.filePermissions

        // Calculate how many items actually have changed names (excluding protected folders and validation errors)
        const changedItemsCount = filesToRename.filter(item => {
            const newName = renameValues[item.id]?.trim()
            const filePath = this.getFilePath(item)
            const permissionInfo = filePermissions.get(filePath)
            const isProtected = permissionInfo?.is_protected || !permissionInfo?.has_permission
            const hasValidationError = validationErrors[item.id]
            return !isProtected && !hasValidationError && newName && newName !== item.name
        }).length

        // Check if there are any validation errors (excluding protected files)
        const hasValidationErrors = filesToRename.some(item => {
            const filePath = this.getFilePath(item)
            const permissionInfo = filePermissions.get(filePath)
            const isProtected = permissionInfo?.is_protected || !permissionInfo?.has_permission
            return !isProtected && validationErrors[item.id]
        })

        return (
            <Modal
                visible={this.state.showRenameModal}
                onDismiss={closeRenameModal}
                size="large"
                header={`Rename ${filesToRename.length === 1 ? 'item' : `${filesToRename.length} items`}`}
                footer={
                    <Box float="right">
                        <SpaceBetween size="xs" direction="horizontal">
                            <Button variant="normal" onClick={closeRenameModal}>
                                Cancel
                            </Button>
                            <Button
                                variant="primary"
                                onClick={handleBulkRename}
                                disabled={hasValidationErrors || changedItemsCount === 0}
                            >
                                {hasValidationErrors
                                    ? 'Fix invalid names'
                                    : changedItemsCount === 0
                                        ? 'No changes'
                                        : `Rename ${changedItemsCount} item${changedItemsCount === 1 ? '' : 's'}`
                                }
                            </Button>
                        </SpaceBetween>
                    </Box>
                }
            >
                <SpaceBetween size="m">
                    <Box>
                        <p>Enter new names for the selected {filesToRename.length === 1 ? 'item' : 'items'}:</p>
                    </Box>

                    <Table
                        columnDefinitions={[
                            {
                                id: 'current',
                                header: 'Current name',
                                cell: (item: FileBrowserEntry) => {
                                    const filePath = this.getFilePath(item)
                                    const permissionInfo = filePermissions.get(filePath)
                                    const isProtected = permissionInfo?.is_protected
                                    const hasPermission = permissionInfo?.has_permission
                                    return (
                                        <Box fontWeight="bold">
                                            <SpaceBetween size="xs" direction="horizontal">
                                                <FontAwesomeIcon icon={entryIcon(item)} fixedWidth={true}/>
                                                <Box>
                                                    {item.name}
                                                </Box>
                                            </SpaceBetween>
                                            {isProtected && (
                                                <StatusIndicator type="warning">Protected, cannot be renamed</StatusIndicator>
                                            )}
                                            {!isProtected && !hasPermission && (
                                                <StatusIndicator type="error">Permission denied</StatusIndicator>
                                            )}
                                        </Box>
                                    )
                                },
                                width: '50%'
                            },
                            {
                                id: 'new',
                                header: 'New name',
                                cell: (item: FileBrowserEntry) => {
                                    const filePath = this.getFilePath(item)
                                    const permissionInfo = filePermissions.get(filePath)
                                    const isProtected = permissionInfo?.is_protected
                                    const hasPermission = permissionInfo?.has_permission
                                    const isDisabled = isProtected || !hasPermission
                                    const validationError = validationErrors[item.id]
                                    const hasError = !isDisabled && !!validationError
                                    return (
                                        <FormField
                                            errorText={hasError ? validationError : undefined}
                                        >
                                            <Input
                                                value={renameValues[item.id] || item.name}
                                                onChange={(e) => updateRenameValue(item.id, e.detail.value)}
                                                placeholder={isDisabled ? "Cannot rename" : "New name"}
                                                disabled={isDisabled}
                                                invalid={hasError}
                                            />
                                        </FormField>
                                    )
                                },
                                width: '50%'
                            }
                        ]}
                        items={filesToRename}
                        variant="borderless"
                        wrapLines={false}
                    />
                </SpaceBetween>
            </Modal>
        )
    }

    closeRenameModal = () => {
        this.setState({
            filesToRename: [],
            showRenameModal: false,
            renameFormValues: {},
            renameValidationErrors: {},
            filePermissions: new Map<string, any>()
        })
    }



    checkRenamePermissions(selectedFiles: FileBrowserEntry[]) {
        const filePaths = selectedFiles.map(file => this.getFilePath(file))

        this.fileBrowserClient().checkFilesPermissions({
            files: filePaths,
            operation: 'rename'
        }).then(result => {
            if (!result.results) {
                this.showToast('rename-check-error', 'Failed to check permissions: No results returned')
                return
            }



            // Store permission results to avoid duplicate API calls
            const permissionMap = new Map<string, any>()
            result.results.forEach(result => {
                if (result.file) {
                    permissionMap.set(result.file, result)
                }
            })

            // Proceed with rename for all selected files (protected ones will be handled in the modal)
            const initialValues: {[fileId: string]: string} = {}
            selectedFiles.forEach(file => {
                initialValues[file.id] = file.name
            })
            this.setState({
                filesToRename: selectedFiles,
                showRenameModal: true,
                renameFormValues: initialValues,
                renameValidationErrors: {},
                filePermissions: permissionMap
            })
        }).catch(error => {
            this.showToast('rename-check-error', `Failed to check permissions: ${error.message}`)
        })
    }

    getDeleteFileConformModal(): IdeaConfirm {
        return this.deleteFileConfirmModal.current!
    }

    buildDeleteFileConfirmModal() {
        return (
            <IdeaConfirm
                ref={this.deleteFileConfirmModal}
                title={`Delete ${this.state.filesToDelete.length.toLocaleString()} ${this.state.filesToDelete.length === 1 ? 'file' : 'files'}`}
                onCancel={() => {
                    this.setState({
                        filesToDelete: [],
                        showDeleteConfirmModal: false
                    })
                }}
                onConfirm={() => {
                    const toDelete: string[] = []
                    this.state.filesToDelete.forEach((file) => {
                        toDelete.push(this.getFilePath(file))
                    })
                    this.fileBrowserClient().deleteFiles({
                        files: toDelete
                    }).then(() => {
                        this.listFiles(this.getCwd()).finally()
                    }).catch(error => {
                        if (error.errorCode === 'UNAUTHORIZED_ACCESS') {
                            this.showToast(error.errorCode, 'Permission denied')
                        } else if (error.errorCode === 'FILE_OPERATION_TIMED_OUT') {
                            this.showToast(error.errorCode, 'Deleting took too long and was stopped. Some files may already be gone.')
                        } else {
                            this.showToast(error.errorCode, error.message)
                        }
                        // whatever could be deleted is gone
                        this.listFiles(this.getCwd()).finally()
                    })
                }}>
                <p>Delete these files? This cannot be undone.</p>
                {this.state.filesToDelete.slice(0, MAX_NAMES_TO_DELETE_SHOWN).map((file, index) => {
                    return <li key={index}>{file.name}</li>
                })}
                {this.state.filesToDelete.length > MAX_NAMES_TO_DELETE_SHOWN && (
                    <li>and {(this.state.filesToDelete.length - MAX_NAMES_TO_DELETE_SHOWN).toLocaleString()} more</li>
                )}
            </IdeaConfirm>
        )
    }

    deleteFiles(files: FileBrowserEntry[]) {
        if (files.some(file => file.isDir)) return
        this.setState({
            filesToDelete: files,
            showDeleteConfirmModal: true
        }, () => {
            this.getDeleteFileConformModal().show()
        })
    }

    onDownloadPrivateKey = (keyFormat: 'pem' | 'ppk') => {
        const state: any = {}
        if (keyFormat === 'pem') {
            state.downloadPemLoading = true
        } else if (keyFormat === 'ppk') {
            state.downloadPpkLoading = true
        }
        this.setState(state, () => {
            AppContext.get().auth().downloadPrivateKey(keyFormat).catch(error => {
                this.showToast('key-download-error', describeKeyFailure(error))
            }).finally(() => {
                const state: any = {}
                if (keyFormat === 'pem') {
                    state.downloadPemLoading = false
                } else if (keyFormat === 'ppk') {
                    state.downloadPpkLoading = false
                }
                this.setState(state)
            })
        })
    }

    isSchedulerDeployed(): boolean {
        return AppContext.get().getClusterSettingsService().isSchedulerDeployed()
    }

    copyPath(file: FileBrowserEntry) {
        const path = this.getFilePath(file)
        Utils.copyToClipBoard(path).then(status => {
            if (status) {
                this.showToast(path, `${file.name} path copied to clipboard`, 'info')
            }
        })
    }

    tailFile(file: FileBrowserEntry) {
        Utils.openNewTab(`/#/home/file-browser/tail?file=${encodePath(this.getFilePath(file))}&cwd=${encodePath(this.getCwd())}`)
    }

    buildCannotOpenModal() {
        const cannotOpen = this.state.cannotOpen
        if (cannotOpen == null) {
            return null
        }
        const file = cannotOpen.file
        const close = () => this.setState({cannotOpen: null})
        return (
            <Modal
                visible={true}
                onDismiss={close}
                header="This file can't be opened here"
                footer={
                    <Box float="right">
                        <SpaceBetween size="xs" direction="horizontal">
                            <Button onClick={close}>Close</Button>
                            {cannotOpen.reason === 'large' && <Button onClick={() => {
                                close()
                                this.tailFile(file)
                            }}>Watch the end of the file</Button>}
                            <Button variant="primary" onClick={() => {
                                close()
                                this.downloadFiles([file])
                            }}>Download</Button>
                        </SpaceBetween>
                    </Box>
                }
            >
                {cannotOpen.reason === 'large'
                    ? `${file.name} is ${formatFileSize(file.size)}. Files up to 5 MB can be opened here.`
                    : `${file.name} isn't a text file, so it can't be shown here.`}
            </Modal>
        )
    }

    /** Right-clicking outside the selection moves the selection to that entry. */
    selectForContextMenu(entry: FileBrowserEntry, tab: 'files' | 'favorites') {
        const selected = (tab === 'files') ? this.state.selectedFiles : this.state.selectedFavorites
        if (selected.some((item) => entryKey(item) === entryKey(entry))) {
            return
        }
        if (tab === 'files') {
            this.setState({selectedFiles: [entry]})
        } else {
            this.setState({selectedFavorites: [entry]})
        }
    }

    filesActions(): FileBrowserAction[] {
        const selected = this.state.selectedFiles
        const any = selected.length > 0
        // these act on one thing: offered for several, they would quietly use only the first
        const one = selected.length === 1
        const oneFile = one && !selected[0].isDir
        const actions: FileBrowserAction[] = [
            {id: 'open', text: 'Open', enabled: one, run: () => this.onOpenSelection(selected[0])},
            {id: 'download', text: 'Download files', enabled: any, run: () => this.downloadFiles(selected)},
            {id: 'copy', text: 'Copy path', enabled: one, run: () => this.copyPath(selected[0])},
            {id: 'rename', text: 'Rename', enabled: any, run: () => this.checkRenamePermissions(selected)},
            {id: 'favorite', text: 'Add to favorites', enabled: any, run: () => selected.forEach((file) => this.addFavorite(file))}
        ]
        if (this.isSchedulerDeployed()) {
            actions.push({
                id: 'submit-job',
                text: 'Submit job',
                enabled: oneFile,
                run: () => this.props.navigate(`/soca/jobs/submit-job?input_file=${encodePath(this.getFilePath(selected[0]))}`)
            })
        }
        actions.push(
            {id: 'tail', text: 'Watch end of file', enabled: oneFile, run: () => this.tailFile(selected[0])},
            {id: 'workbench', text: 'Open in script editor', enabled: oneFile, run: () => this.openFileInScriptWorkbench(selected[0])},
            {id: 'delete', text: 'Delete files', enabled: any && selected.every(file => !file.isDir), run: () => this.deleteFiles(selected)},
            {id: 'delete-folder', text: 'Delete folder', enabled: selected.length === 1 && !!selected[0].isDir,
                run: () => this.setState({folderToDelete: {path: this.getFilePath(selected[0]), name: selected[0].name!}})}
        )
        return actions
    }

    favoritesActions(): FileBrowserAction[] {
        const selected = this.state.selectedFavorites
        const any = selected.length > 0
        const actions: FileBrowserAction[] = [
            {id: 'open', text: 'Open', enabled: selected.length === 1, run: () => this.onOpenFavorite(selected[0])},
            {
                id: 'remove-favorite',
                text: 'Remove from favorites',
                enabled: any,
                run: () => selected.forEach((file) => this.removeFavorite(file))
            }
        ]
        if (this.isSchedulerDeployed()) {
            actions.push({
                id: 'submit-job',
                text: 'Submit job',
                enabled: selected.length === 1 && !selected[0].isDir,
                run: () => this.props.navigate(`/soca/jobs/submit-job?input_file=${encodePath(selected[0].path ?? '')}`)
            })
        }
        return actions
    }

    buildMenuItems(actions: FileBrowserAction[], extra: FileBrowserMenuItem[] = []): FileBrowserMenuItem[] {
        const items: FileBrowserMenuItem[] = actions
            .filter((action) => action.enabled)
            .map((action) => ({id: action.id, text: action.text, onClick: action.run}))
        return items.concat(extra)
    }

    /** Every action in one menu, disabled until the selection allows it, plus any view options. */
    buildDropdown(actions: FileBrowserAction[], extra: ButtonDropdownProps.ItemOrGroup[] = [], onExtraClick?: (id: string) => void) {
        const items: ButtonDropdownProps.ItemOrGroup[] = actions.map((action) => ({id: action.id, text: action.text, disabled: !action.enabled}))
        return (
            <ButtonDropdown
                items={items.concat(extra)}
                onItemClick={(event) => {
                    const action = actions.find((candidate) => candidate.id === event.detail.id)
                    if (action != null) {
                        action.run()
                    } else {
                        onExtraClick?.(event.detail.id)
                    }
                }}
            >
                Actions
            </ButtonDropdown>
        )
    }

    buildPathNav() {
        return (
            <FileBrowserPath
                path={this.getCwd()}
                home={this.state.homeDir}
                onNavigate={(path) => this.listFiles(path).finally()}
                onSubmitPath={(path) => this.navigateToTypedPath(path)}
            />
        )
    }

    buildFilesToolbar(actions: FileBrowserAction[]) {
        const viewOptions: ButtonDropdownProps.ItemOrGroup[] = [{
            text: 'View',
            items: [{id: 'toggle-hidden', itemType: 'checkbox', checked: this.state.showHiddenFiles, text: 'Show hidden files'}]
        }]
        return (
            <SpaceBetween size="xs" direction="horizontal">
                <Button variant="icon" iconName="refresh" ariaLabel="Refresh" onClick={() => this.listFiles(this.getCwd()).finally()}/>
                {this.buildDropdown(actions, viewOptions, () => this.setState({showHiddenFiles: !this.state.showHiddenFiles}))}
                <Button onClick={() => this.getCreateFolderForm().showModal()}>Create folder</Button>
                <Button variant="primary" onClick={() => this.showUploadModal()}>Upload files</Button>
            </SpaceBetween>
        )
    }

    render() {

        const filesActions = this.filesActions()
        const favoritesActions = this.favoritesActions()

        return (
            <IdeaAppLayout
                ideaPageId={this.props.ideaPageId}
                header={<Header variant={"h1"}>Files</Header>}
                toolsOpen={this.props.toolsOpen}
                tools={this.props.tools}
                onToolsChange={this.props.onToolsChange}
                onPageChange={this.props.onPageChange}
                sideNavHeader={this.props.sideNavHeader}
                sideNavItems={this.props.sideNavItems}
                onSideNavChange={this.props.onSideNavChange}
                onFlashbarChange={this.props.onFlashbarChange}
                flashbarItems={this.props.flashbarItems}
                breadcrumbItems={[
                    {
                        text: 'IDEA',
                        href: '#/'
                    },
                    {
                        text: 'Files',
                        href: ''
                    }
                ]}
                contentType={"default"}
                disableContentHeaderOverlap={true}
                content={
                    <div style={{marginTop: '20px'}}>
                        {this.state.showDeleteConfirmModal && this.buildDeleteFileConfirmModal()}
                        {this.state.folderToDelete && <DeleteFolderDialog
                            {...this.state.folderToDelete}
                            home={this.state.homeDir}
                            onClose={() => {
                                this.setState({folderToDelete: null})
                                // a failed attempt may have removed part of the folder
                                this.listFiles(this.getCwd()).finally()
                            }}
                            onDeleted={() => {
                                this.setState({folderToDelete: null})
                                this.listFiles(this.getCwd()).finally()
                            }}/>}
                        {this.buildCreateFolderForm()}
                        {this.buildRenameFileModal()}
                        {this.buildFileEditor()}
                        {this.buildCannotOpenModal()}
                        {/*<Container>*/}
                        {this.state.listError && (
                            <Alert type="error" dismissible={true} onDismiss={() => this.setState({listError: null})}>
                                {this.state.listError}
                            </Alert>
                        )}
                        <Tabs
                            onChange={(event) => {
                                this.setState({
                                    activeTabId: event.detail.activeTabId
                                })
                            }}
                            activeTabId={this.state.activeTabId}
                            tabs={[
                                {
                                    id: 'files',
                                    label: 'My files',
                                    content: (
                                        <FileBrowserTable
                                            title="My files"
                                            entries={this.state.files}
                                            selectedEntries={this.state.selectedFiles}
                                            onSelectionChange={(entries) => this.setState({selectedFiles: entries})}
                                            onOpen={(entry) => this.onOpenSelection(entry)}
                                            onContextMenu={(entry) => this.selectForContextMenu(entry, 'files')}
                                            menuItems={this.buildMenuItems(filesActions, [
                                                {
                                                    id: 'toggle-hidden',
                                                    text: 'Show hidden files',
                                                    onClick: () => this.setState({showHiddenFiles: !this.state.showHiddenFiles})
                                                }
                                            ])}
                                            actions={this.buildFilesToolbar(filesActions)}
                                            path={this.buildPathNav()}
                                            loading={this.state.listing}
                                            emptyText={this.state.listError && !this.state.listingLoaded ? 'This folder could not be opened.' : undefined}
                                            filterScope={this.getCwd()}
                                            onShowHiddenFiles={() => this.setState({showHiddenFiles: true})}
                                            showHiddenFiles={this.state.showHiddenFiles}
                                        />
                                    )
                                },
                                {
                                    id: 'favorites',
                                    label: 'Favorites',
                                    content: (
                                        <FileBrowserTable
                                            title="Favorites"
                                            emptyLabel="No favorites yet."
                                            filterLabel="Filter favorites by name"
                                            entries={this.state.favorites}
                                            selectedEntries={this.state.selectedFavorites}
                                            onSelectionChange={(entries) => this.setState({selectedFavorites: entries})}
                                            onOpen={(entry) => this.onOpenFavorite(entry)}
                                            onContextMenu={(entry) => this.selectForContextMenu(entry, 'favorites')}
                                            menuItems={this.buildMenuItems(favoritesActions)}
                                            actions={this.buildDropdown(favoritesActions)}
                                        />
                                    )
                                },
                                {
                                    id: 'file-transfer',
                                    label: 'File transfer',
                                    disabled: !this.state.sshAccess,
                                    content: (
                                        <SpaceBetween size={"s"}>
                                            <Container header={<Header variant="h3" description="Use one of these to move large files to your cluster.">File transfer method</Header>}>
                                                <Tiles value={this.state.fileTransferMethod}
                                                       columns={AppContext.get().auth().isAdmin() ? 3 : 2}
                                                       items={[
                                                           {
                                                               label: 'FileZilla',
                                                               description: 'Available for Windows, macOS and Linux',
                                                               value: 'file-zilla'
                                                           },
                                                           {
                                                               label: 'WinSCP',
                                                               description: 'Available for Windows only',
                                                               value: 'winscp'
                                                           },
                                                           // setting this up takes the AWS console, so it is an administrator's job
                                                           ...(AppContext.get().auth().isAdmin() ? [{
                                                               label: 'AWS Transfer',
                                                               description: 'Your IDEA cluster must be using Amazon EFS to use AWS Transfer',
                                                               value: 'aws-transfer'
                                                           }] : [])
                                                       ]}
                                                       onChange={(event) => {
                                                           this.setState({
                                                               fileTransferMethod: event.detail.value
                                                           })
                                                       }}
                                                />
                                            </Container>
                                            {this.state.fileTransferMethod === 'file-zilla' && <Container header={<Header variant={"h3"}>FileZilla</Header>}>
                                                <TextContent>
                                                <SpaceBetween size={"s"}>
                                                    <Box>
                                                        <h4>Step 1: Download FileZilla</h4>
                                                        <ul>
                                                            <li>
                                                                <Link external={true} href={"https://filezilla-project.org/download.php?platform=osx"}>Download FileZilla (macOS)</Link>
                                                            </li>
                                                            <li>
                                                                <Link external={true} href={"https://filezilla-project.org/download.php?platform=win64"}>Download FileZilla (Windows)</Link>
                                                            </li>
                                                            <li>
                                                                <Link external={true} href={"https://filezilla-project.org/download.php?platform=linux"}>Download FileZilla (Linux)</Link>
                                                            </li>
                                                        </ul>
                                                    </Box>
                                                    <Box>
                                                        <h4>Step 2: Download key file</h4>
                                                        <SpaceBetween size={"l"} direction={"horizontal"}>
                                                            <Button variant={"normal"} onClick={() => this.onDownloadPrivateKey('pem')} loading={this.state.downloadPemLoading}><FontAwesomeIcon icon={faDownload}/> Download key file (.pem, macOS and Linux)</Button>
                                                            <Button variant={"normal"} onClick={() => this.onDownloadPrivateKey('ppk')} loading={this.state.downloadPpkLoading}><FontAwesomeIcon icon={faDownload}/> Download key file (.ppk, Windows)</Button>
                                                        </SpaceBetween>
                                                    </Box>
                                                    <Box>
                                                        <h4>Step 3: Configure FileZilla</h4>
                                                        <p>Open FileZilla and select <b>File &gt; Site Manager</b> to create a new Site using below options:</p>
                                                        <Container>
                                                            <ColumnLayout columns={2}>
                                                                <KeyValue title="Host" value={this.state.sshHostIp}/>
                                                                <KeyValue title="Port" value={"22"}/>
                                                                <KeyValue title="Protocol" value={"SFTP"}/>
                                                                <KeyValue title="Logon Type" value={"Key File"}/>
                                                                <KeyValue title="User" value={AppContext.get().auth().getUsername()}/>
                                                                <KeyValue title="Key File" value={"/path/to/key-file (downloaded in Step 2)"}/>
                                                            </ColumnLayout>
                                                        </Container>
                                                        <p><b>Save</b> the settings and click <b>Connect</b></p>
                                                    </Box>
                                                    <Box>
                                                        <h4>Step 4: Connect and transfer files</h4>
                                                        <p>During your first connection, you will be asked whether or not you want to trust {this.state.sshHostIp}. Check "Always Trust this Host" and Click "Ok".</p>
                                                        <p>Once connected, simply drag & drop to upload/download files.</p>
                                                    </Box>
                                                </SpaceBetween>
                                                </TextContent>
                                            </Container>}
                                            {this.state.fileTransferMethod === 'winscp' && <Container header={<Header variant={"h3"}>WinSCP (Windows)</Header>}>
                                                <TextContent>
                                                <SpaceBetween size={"s"}>
                                                    <Box>
                                                        <Alert onDismiss={() => false}
                                                               dismissAriaLabel="Close alert"
                                                               header="Info">
                                                            WinSCP is only available on Windows. Use FileZilla if you are on Linux or a Mac.
                                                        </Alert>
                                                        <h4>Step 1: Download WinSCP</h4>
                                                        <ul>
                                                            <li>
                                                                <Link external={true} href={"https://winscp.net/eng/download.php"}>Download WinSCP (Windows)</Link>
                                                            </li>
                                                        </ul>
                                                    </Box>
                                                    <Box>
                                                        <h4>Step 2: Download key file</h4>
                                                        <SpaceBetween size={"l"} direction={"horizontal"}>
                                                            <Button variant={"normal"} onClick={() => this.onDownloadPrivateKey('pem')} loading={this.state.downloadPemLoading}><FontAwesomeIcon icon={faDownload}/> Download key file (.pem, macOS and Linux)</Button>
                                                            <Button variant={"normal"} onClick={() => this.onDownloadPrivateKey('ppk')} loading={this.state.downloadPpkLoading}><FontAwesomeIcon icon={faDownload}/> Download key file (.ppk, Windows)</Button>
                                                        </SpaceBetween>
                                                    </Box>
                                                    <Box>
                                                        <h4>Step 3: Configure WinSCP</h4>
                                                        <p>Open WinSCP and select <b>File &gt; Site Manager</b> to create a new Site using below options:</p>
                                                        <Container>
                                                            <ColumnLayout columns={2}>
                                                                <KeyValue title="Host Name" value={this.state.sshHostIp}/>
                                                                <KeyValue title="Port Number" value={"22"}/>
                                                                <KeyValue title="File Protocol" value={"SFTP"}/>
                                                                <KeyValue title="Logon Type" value={"Key File"}/>
                                                                <KeyValue title="User Name" value={AppContext.get().auth().getUsername()}/>
                                                                <KeyValue title="Password" value={"Leave Blank and click Advanced. Click SSH > Authentication Page and load your key under Private Key File."}/>

                                                                <KeyValue title="Key File" value={"/path/to/key-file (downloaded in Step 2)"}/>
                                                            </ColumnLayout>
                                                        </Container>
                                                        <p><b>Save</b> the settings and click <b>Connect</b></p>
                                                    </Box>
                                                    <Box>
                                                        <h4>Step 4: Connect and transfer files</h4>
                                                        <p>During your first connection, you will be asked whether or not you want to trust {this.state.sshHostIp}. Check "Always Trust this Host" and Click "Ok".</p>
                                                        <p>Once connected, simply drag & drop to upload/download files.</p>
                                                    </Box>
                                                </SpaceBetween>
                                                </TextContent>
                                            </Container>}
                                            {this.state.fileTransferMethod === 'aws-transfer' && <Container header={<Header variant={"h3"}>AWS Transfer</Header>}>
                                                <TextContent>
                                                <SpaceBetween size={"s"}>
                                                    <Box>
                                                        <Alert onDismiss={() => false}
                                                               dismissAriaLabel="Close alert"
                                                               header="Limitations">
                                                            Your IDEA cluster must be using Amazon EFS to use AWS Transfer
                                                        </Alert>
                                                        <h4>Step 1: Configure AWS Transfer</h4>
                                                        <ul>
                                                            <li>Open AWS Console and navigate to the service named <b>AWS Transfer Family</b> then click <b>Create Server</b></li>
                                                            <li>Select <b>SFTP (SSH File Transfer Protocol) - file transfer over Secure Shell</b></li>
                                                            <li>Select <b>Service Managed</b> as identity provider</li>
                                                            <li>Choose <b>Endpoint type</b> depending on your setup. It's recommended to use <b>VPC hosted</b> for IP restrictions via your security groups. <b>Publicly accessible</b> means your AWS Transfer endpoint won't be protected by IP safelist.</li>
                                                            <li>Select <b>Amazon EFS</b> as Domain</li>
                                                            <li>Select <b>Create a new role</b> and use the latest <b>TransferSecurityPolicy</b> available</li>
                                                            . Leave everything else as default.
                                                            <li>In the <b>Review and create</b> section click <b>Create server</b></li>
                                                        </ul>

                                                        <h4>Step 2: Create an IAM role for your AWS Transfer users</h4>
                                                        <ul>
                                                            <li>Open AWS Console and navigate to the service named <b>IAM</b> then click <b>Roles</b> on the left sidebar and finally click <b>Create Role</b></li>
                                                            <li>Select <b>AWS Service</b> as Trusted Entity Type and select <b>Transfer</b> as Use Case</li>
                                                            <li>Select the AWS managed policy named <b>AmazonElasticFileSystemClientReadWriteAccess</b></li>
                                                            <li>Select a Role name (for example <b>TransferEFSClient</b> and save it</li>
                                                        </ul>

                                                        <h4>Step 3: Get your public key</h4>
                                                        <ul>
                                                            <li>Download your <b>public</b> SSH key. You can retrieve it under <b>$HOME/.ssh/id_rsa.pub</b></li>
                                                        </ul>

                                                        <h4>Step 4: Download your private key file</h4>
                                                        <SpaceBetween size={"l"} direction={"horizontal"}>
                                                            <Button variant={"normal"} onClick={() => this.onDownloadPrivateKey('pem')} loading={this.state.downloadPemLoading}><FontAwesomeIcon icon={faDownload}/> Download key file (.pem, macOS and Linux)</Button>
                                                        </SpaceBetween>

                                                        <h4>Step 5: Register your AWS Transfer user</h4>
                                                        <Alert onDismiss={() => false}
                                                               dismissAriaLabel="Close alert"
                                                               header="User Information">
                                                            <p>You will need your user UID/GID. You can retrieve this value by typing <b>id {AppContext.get().auth().getUsername()}</b> on your IDEA cluster.

                                                                In the example below, the UID is 5001 and GID is also 5001</p>
                                                            <code>
                                                                #id {AppContext.get().auth().getUsername()} <br/>

                                                                uid=5001({AppContext.get().auth().getUsername()}) gid=5001({AppContext.get().auth().getUsername()}) groups=5001({AppContext.get().auth().getUsername()})
                                                            </code>
                                                        </Alert>

                                                        <p>Open AWS Console and navigate to the service named <b>AWS Transfer Family</b> select the server you have created and click <b>Add User</b> and enter the following information.</p>

                                                        <Container>
                                                            <ColumnLayout columns={2}>
                                                                <KeyValue title="Username" value={AppContext.get().auth().getUsername()}/>
                                                                <KeyValue title="User ID" value={"The Posix UID of your user you have retrieved via `id` command"}/>
                                                                <KeyValue title="Group ID" value={"The Posix GID of your user you have retrieved via `id` command"}/>
                                                                <KeyValue title="Role" value={"The IAM role you have created (ex: TransferEFSClient)"}/>
                                                                <KeyValue title="Home Directory" value={"Select your IDEA EFS filesystem you have mounted as /data"}/>
                                                                <KeyValue title="SSH Public Key" value={"The content of your SSH public key retrieved during Step 3"}/>
                                                            </ColumnLayout>
                                                        </Container>

                                                        <h4>Step 6: Test</h4>
                                                        <p>Open AWS Console and navigate to the service named <b>AWS Transfer Family</b> select the server you have created, and retrieve the <b>Endpoint</b> under <b>Endpoint Details</b>.</p>
                                                        <Alert onDismiss={() => false}
                                                               dismissAriaLabel="Close alert"
                                                               header="Endpoint Information">
                                                            Your endpoint use the following syntax: <b>{"s-<UNIQUE_ID>"}-.server.transfer.{"<AWS_REGION>"}.amazonaws.com</b>
                                                        </Alert>

                                                        <p>Connect to your AWS Transfer endpoint using your favorite FTP application via command line such as: </p>
                                                        <code>
                                                            sftp -i {"/PATH/TO/PRIVATE_KEY"} {AppContext.get().auth().getUsername()}@{"<AWS_TRANSFER_ENDPOINT>"}
                                                        </code>
                                                        <p>Alternatively, you can use WinSCP/FileZilla. Refer to the instructions available on this website and use your AWS Transfer endpoint as hostname.</p>
                                                    </Box>
                                                </SpaceBetween>
                                                </TextContent>
                                            </Container>}
                                        </SpaceBetween>

                                    )
                                },
                            ]}/>
                        {/*</Container>*/}
                    </div>
                }/>
        )
    }
}

export default withRouter(IdeaFileBrowser)
