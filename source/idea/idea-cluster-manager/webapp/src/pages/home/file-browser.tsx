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
import {Alert, Box, Button, CodeEditor, ColumnLayout, Container, Header, Link, Modal, SpaceBetween, StatusIndicator, Tabs, Tiles, Table, Input, FormField} from "@cloudscape-design/components";
import {toast} from "react-toastify";
import {
    ChonkyActions,
    FileData,
    FileNavbar,
    FileBrowser,
    FileToolbar,
    FileList,
    FileContextMenu,
    defineFileAction
} from "@aperturerobotics/chonky";

import 'ace-builds/css/ace.css';
import 'ace-builds/css/theme/dawn.css';
import 'ace-builds/css/theme/github_light_default.css';
import 'ace-builds/css/theme/github_dark.css';

import {CodeEditorProps} from "@cloudscape-design/components/code-editor/interfaces";
import {faDownload, faMicrochip, faRedo, faStar, faTerminal, faTrash, faEdit, faPencilAlt} from "@fortawesome/free-solid-svg-icons";
import Uppy from "@uppy/core";
import XHRUpload from "@uppy/xhr-upload";
import Dashboard from "@uppy/dashboard";
import '@uppy/core/dist/style.css'
import '@uppy/dashboard/dist/style.css'
import IdeaForm from "../../components/form";
import {IdeaSideNavigationProps} from "../../components/side-navigation";
import IdeaAppLayout, {IdeaAppLayoutProps} from "../../components/app-layout";
import {FontAwesomeIcon} from "@fortawesome/react-fontawesome";
import {KeyValue} from "../../components/key-value";
import {Constants} from "../../common/constants";
import IdeaConfirm from "../../components/modals";
import {withRouter} from "../../navigation/navigation-utils";

// Filter out JSS-related warning messages
const originalConsoleWarn = console.warn;
console.warn = function filterWarnings(msg, ...args) {
    // Check if the message includes the JSS warning we want to hide
    if (typeof msg === 'string' && msg.includes('[JSS] <Hook />\'s styles function doesn\'t rely on the "theme" argument')) {
        return;
    }
    originalConsoleWarn(msg, ...args);
};

export interface IdeaFileBrowserProps extends IdeaAppLayoutProps, IdeaSideNavigationProps {

}

export interface IdeaFileBrowserState {
    files: FileData[]
    favorites: FileData[]
    folderChain: FileData[]
    filesToDelete: FileData[]
    showDeleteConfirmModal: boolean
    editorOpen: boolean
    fileUploadResult: any
    activeTabId: string
    downloadPemLoading: boolean
    downloadPpkLoading: boolean
    sshHostIp: string
    sshAccess: boolean
    fileTransferMethod: string
    filesToRename: FileData[]
    showRenameModal: boolean
    renameFormValues: {[fileId: string]: string}
    renameValidationErrors: {[fileId: string]: string}
    filePermissions: Map<string, any>
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

const CustomActionSubmitJob = defineFileAction({
    id: 'soca_submit_job',
    button: {
        name: 'Submit Job',
        toolbar: true,
        contextMenu: true,
        icon: faMicrochip
    }
})

const CustomActionOpenInScriptEditor = defineFileAction({
    id: 'soca_open_in_script_workbench',
    button: {
        name: 'Open in Script Workbench',
        toolbar: false,
        contextMenu: true,
        icon: faEdit
    }
})

const CustomActionFavorite = defineFileAction({
    id: 'soca_favorite',
    button: {
        name: 'Favorite',
        toolbar: true,
        contextMenu: true,
        icon: faStar
    }
})

const CustomActionRemoveFavorite = defineFileAction({
    id: 'soca_remove_favorite',
    button: {
        name: 'Remove Favorite',
        toolbar: true,
        contextMenu: true,
        icon: faTrash
    }
})

const CustomActionRefresh = defineFileAction({
    id: 'soca_refresh',
    button: {
        name: 'Refresh',
        toolbar: true,
        contextMenu: true,
        icon: faRedo
    }
})

const CustomActionTailLogFile = defineFileAction({
    id: 'soca_tail_log_file',
    button: {
        name: 'Tail File',
        toolbar: false,
        contextMenu: true,
        icon: faTerminal
    }
})

const CustomActionRenameFile = defineFileAction({
    id: 'soca_rename_file',
    button: {
        name: 'Rename',
        toolbar: true,
        contextMenu: true,
        icon: faPencilAlt
    }
})

/*
 * Override the Chonky default of showing hidden files.
 * We want to default to not showing hidden files.
 */
const CustomActionToggleHiddenFiles = (
    JSON.parse(JSON.stringify(ChonkyActions.ToggleHiddenFiles))
)
CustomActionToggleHiddenFiles.option.defaultValue = false
CustomActionToggleHiddenFiles.button.toolbar = true

const ACTIONS = [
    ChonkyActions.OpenFiles,
    ChonkyActions.UploadFiles,
    ChonkyActions.CreateFolder,
    ChonkyActions.DeleteFiles,
    ChonkyActions.CopyFiles,
    ChonkyActions.DownloadFiles,
    CustomActionFavorite,
    CustomActionRefresh,
    CustomActionRenameFile,
    CustomActionTailLogFile,
    CustomActionOpenInScriptEditor,
    CustomActionToggleHiddenFiles
]

const FAVORITE_ACTIONS = [
    ChonkyActions.OpenFiles,
    CustomActionRemoveFavorite
]

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
            import('ace-builds/webpack-resolver').then(() => {
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
                <small>{this.state.filepath}</small>
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
                                        showStatus(false, 'Permission Denied')
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

class IdeaFileBrowser extends Component<IdeaFileBrowserProps, IdeaFileBrowserState> {

    fileEditor: RefObject<IdeaFileEditorModal>
    _fileBrowserClient: FileBrowserClient
    createFolderForm: RefObject<IdeaForm>
    deleteFileConfirmModal: RefObject<IdeaConfirm>
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
            folderChain: [],
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
            filesToRename: [],
            showRenameModal: false,
            renameFormValues: {},
            renameValidationErrors: {},
            filePermissions: new Map<string, any>()
        }
        this._fileBrowserClient = AppContext.get().client().fileBrowser()
        this.createFolderForm = React.createRef()
        this.deleteFileConfirmModal = React.createRef()
    }

    getCreateFolderForm(): IdeaForm {
        return this.createFolderForm.current!
    }



    adjustFileBrowserHeight() {
        setTimeout(() => {
            let fileBrowsers = document.getElementsByClassName('soca-file-browser')
            for (let i = 0; i < fileBrowsers.length; i++) {
                let fileBrowser = fileBrowsers[i]
                fileBrowser.setAttribute('style', `height: 100vh`)
            }
        }, 100)
    }

    componentDidMount() {
        AppContext.get().getClusterSettingsService().getModuleSettings(Constants.MODULE_BASTION_HOST).then(moduleInfo => {
            this.setState({
                sshHostIp: Utils.asString(moduleInfo.public_ip),
                sshAccess: true
            })
        }).catch(error => {
            if (error.errorCode === 'MODULE_NOT_FOUND') {
                this.setState({
                    sshAccess: false
                })
            }
        })
        this.adjustFileBrowserHeight()
        this.listFavorites()
        const cwd = this.props.searchParams.get('cwd')
        this.listFiles((cwd) ? cwd : undefined).finally()
    }

    componentWillUnmount() {
        let appContent = document.getElementsByClassName('soca-app-content')[0]
        appContent.removeAttribute('style')
    }

    fileBrowserClient(): FileBrowserClient {
        return this._fileBrowserClient
    }

    convert(payload?: ListFilesResult): FileData[] {
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

    buildFolderChain(cwd: string): FileData[] {
        const tokens = cwd.split('/')
        const result: FileData[] = []
        let index = 0
        result.push({id: 'root', name: 'root', isDir: true, folderChain: true, index: index++})
        tokens.forEach((token) => {
            if (Utils.isEmpty(token)) {
                return true
            }
            result.push({id: token, name: token, isDir: true, folderChain: true, index: index++})
        })
        return result
    }

    getCwd(index: number): string {
        const tokens: string[] = []
        const folderChain = this.state.folderChain
        if (index === 0) {
            return '/'
        }
        for (let i = 1; i < folderChain.length; i++) {
            const entry = folderChain[i]
            tokens.push(entry.name)
            if (index === entry.index) {
                break
            }
        }
        return '/' + tokens.join('/')
    }

    checkFileSize(file: FileData): boolean {
        const maxFileSize = 5 * 1024 * 1024; // 5MB in bytes
        if (file.size && file.size > maxFileSize) {
            const fileSizeMB = (file.size / (1024 * 1024)).toFixed(2);
            this.showToast('file-too-large', `File is too large (${fileSizeMB}MB). Only files 5MB or smaller can be opened in the editor.`);
            return false;
        }
        return true;
    }

    showToast(id: string, message: string, type: 'warning' | 'info' = 'warning') {
        if (type === 'warning') {
            toast.warn(message, {
                autoClose: 2000,
                toastId: id
            })
        } else if (type === 'info') {
            toast.info(message, {
                autoClose: 2000,
                toastId: id
            })
        }
    }

    listFiles(cwd?: string): Promise<boolean> {
        return this.fileBrowserClient().listFiles({
            cwd: cwd
        }).then((result: ListFilesResult) => {
            this.props.searchParams.set('cwd', result.cwd!)
            this.props.setSearchParams(this.props.searchParams)
            this.setState({
                files: this.convert(result),
                folderChain: this.buildFolderChain(result.cwd!)
            })
        }).then(() => {
            return true
        })
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

    addFavorite(file: FileData) {
        const favorites = [...this.state.favorites]
        const path = `${this.getCwd(-1)}/${file.name}`
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

    removeFavorite(file: FileData) {
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

    getFilePath(file: FileData): string {
        let path = file.path
        if (path == null) {
            let cwd = this.getCwd(-1)
            if (cwd === '/') {
                path = '/' + file.name
            } else {
                path = cwd + '/' + file.name
            }
        }
        return path
    }

    downloadFiles(files: FileData[]) {

        const download = (file: string, skipFlashbar = false) => {
            const tokens = file.split('/')
            const fileName = tokens[tokens.length - 1]

            // Get a temporary signed download URL from the server
            AppContext.get().auth().getAccessToken().then(accessToken => {
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
                    throw new Error(`Failed to generate download URL: ${response.status}`)
                }
                return response.json()
            }).then(result => {
                // Use the temporary signed URL for native browser download
                const link = document.createElement('a')
                link.href = result.download_url
                link.setAttribute('download', fileName)
                link.style.display = 'none'
                document.body.appendChild(link)
                link.click()
                document.body.removeChild(link)
            }).catch(error => {
                console.error('Download failed:', error)
                const errorMessage = error instanceof Error ? error.message : 'Unknown error'
                if (!skipFlashbar) {
                    this.showToast('download-error', `Failed to download ${fileName}: ${errorMessage}`)
                }
            })
        }

        // Determine if we need to show flashbar (for directories, multiple files, or large single files)
        const needsFlashbar = files.length > 1 ||
                             files.some(file => file.isDir) ||
                             files.some(file => file.size && file.size > 10 * 1024 * 1024) // 10MB threshold

        if (files.length === 1 && !needsFlashbar) {
            // Simple single file download without flashbar
            download(this.getFilePath(files[0]))
        } else {
            // Show loading feedback for downloads that might take time
            const downloadId = `download-${Date.now()}-${Math.random().toString(36).substr(2, 9)}`

            let contentMessage = ''
            if (files.length === 1 && files[0].isDir) {
                contentMessage = `Preparing download archive for directory "${files[0].name}"... This may take several minutes for directories with large files.`
            } else if (files.length === 1) {
                contentMessage = `Preparing download for large file "${files[0].name}"... This may take several minutes.`
            } else {
                contentMessage = `Preparing download archive for ${files.length} files... This may take several minutes for large files.`
            }

            const preparingItem = {
                type: 'info' as const,
                loading: true,
                header: 'Preparing Download',
                content: contentMessage,
                dismissible: false,
                customId: downloadId
            }

            // Add the preparing item to current flashbar items
            this.props.onFlashbarChange({
                items: [...(this.props.flashbarItems || []), preparingItem]
            })

            if (files.length === 1 && !files[0].isDir) {
                // Single file download with flashbar
                download(this.getFilePath(files[0]), true)

                // Replace with success message
                const successItem = {
                    type: 'success' as const,
                    header: 'Download Ready',
                    content: 'Your download should start automatically.',
                    dismissible: true,
                    customId: downloadId
                }

                // Replace the preparing item with success
                setTimeout(() => {
                    const currentItems = this.props.flashbarItems || []
                    const updatedItems = currentItems.map(item => {
                        return (item as any).customId === downloadId ? successItem : item
                    })

                    this.props.onFlashbarChange({
                        items: updatedItems
                    })

                    // Auto-remove success message after 5 seconds
                    setTimeout(() => {
                        this.props.onFlashbarChange({
                            items: (this.props.flashbarItems || []).filter(item => (item as any).customId !== downloadId)
                        })
                    }, 5000)
                }, 500) // Small delay to show the preparing state
            } else {
                // Multiple files or directory download - use API
                const file_paths: string[] = []
                files.forEach((file) => file_paths.push(this.getFilePath(file)))
                this.fileBrowserClient().downloadFiles({
                    files: file_paths
                }).then(result => {
                    // Replace with success message
                    const successItem = {
                        type: 'success' as const,
                        header: 'Download Ready',
                        content: 'Download archive is ready! Your download should start automatically.',
                        dismissible: true,
                        customId: downloadId
                    }

                    // Create new items array with the replacement
                    const currentItems = this.props.flashbarItems || []
                    const updatedItems = currentItems.map(item => {
                        return (item as any).customId === downloadId ? successItem : item
                    })

                    // If no item was replaced, add it
                    if (!updatedItems.some(item => (item as any).customId === downloadId)) {
                        updatedItems.push(successItem)
                    }

                    this.props.onFlashbarChange({
                        items: updatedItems
                    })

                    download(result.download_url!, true)

                    // Auto-remove success message after 10 seconds
                    setTimeout(() => {
                        this.props.onFlashbarChange({
                            items: (this.props.flashbarItems || []).filter(item => (item as any).customId !== downloadId)
                        })
                    }, 10000)
                }).catch(error => {
                    console.error('Download preparation failed:', error)
                    // Replace with error message using the same pattern as other error handlers
                    const errorItem = {
                        type: 'error' as const,
                        header: 'Download Failed',
                        content: `Failed to prepare download: ${error.message} (${error.errorCode})`,
                        dismissible: true,
                        customId: downloadId
                    }

                    // Create new items array with the replacement
                    const currentItems = this.props.flashbarItems || []
                    const updatedItems = currentItems.map(item =>
                        (item as any).customId === downloadId ? errorItem : item
                    )

                    // If no item was replaced, add it
                    if (!updatedItems.some(item => (item as any).customId === downloadId)) {
                        updatedItems.push(errorItem)
                    }

                    this.props.onFlashbarChange({
                        items: updatedItems
                    })
                })
            }
        }
    }

    onOpenSelection(payload?: FileData) {
        if (payload == null) {
            return
        }
        if (payload.folderChain) {
            this.listFiles(this.getCwd(payload.index))
                .catch(error => {
                    this.showToast(error.errorCode, error.message)
                })
                .finally()
        } else if (payload.isDir) {
            let cwd = this.getCwd(-1)
            let targetDir = ''
            if (cwd === '/') {
                targetDir = '/' + payload.name
            } else {
                targetDir = cwd + '/' + payload.name
            }
            this.listFiles(targetDir)
                .catch(error => {
                    this.showToast(error.errorCode, error.message)
                })
                .finally()
        } else {
            this.openFile(payload)
        }
    }

    onOpenFavorite(payload?: FileData) {
        if (payload == null) {
            return
        }
        if (payload.isDir) {
            this.listFiles(payload.path).then(() => {
                this.setState({
                    activeTabId: 'files'
                }, () => {
                    this.adjustFileBrowserHeight()
                })
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
                        content: btoa(content)
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

    openFile(file: FileData) {
        // Check file size before attempting to open
        if (!this.checkFileSize(file)) {
            return;
        }

        const path = this.getFilePath(file)
        this.fileBrowserClient().readFile({
            file: path
        }).then(result => {
            this.fileEditor.current?.setContent(path, atob(result.content!))
            this.fileEditor.current?.setLanguage('sh')
            this.setState({
                editorOpen: true
            })
        }).catch(error => {
            if (error.errorCode === 'NOT_A_TEXT_FILE') {
                this.downloadFiles([file])
            } else {
                toast(error.message)
            }
        })
    }

    openFileInScriptWorkbench(file: FileData) {
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
            this.props.navigate(`/home/script-workbench?file=${path}`)
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
                    endpoint: `${httpEndpoint}${FILE_BROWSER_API_PATH}/upload?cwd=${this.getCwd(-1)}`,
                    headers: {
                        'Authorization': `Bearer ${accessToken}`
                    },
                    formData: true,
                    fieldName: 'files[]',
                    method: 'PUT',
                    bundle: true
                })
            const dashboard: Dashboard = uppy.getPlugin('Dashboard')!
            dashboard.openModal()
            uppy.on('complete', () => {
                this.listFiles(this.getCwd(-1)).finally()
            })
        })
    }

    buildCreateFolderForm() {
        return (
            <IdeaForm ref={this.createFolderForm}
                      name="create-folder"
                      modal={true}
                      modalSize="medium"
                      title="Create New Folder"
                      onSubmit={() => {
                          if (!this.getCreateFolderForm().validate()) {
                              return
                          }
                          const values = this.getCreateFolderForm().getValues()
                          this.fileBrowserClient().createFile({
                              cwd: this.getCwd(-1),
                              filename: values.name,
                              is_folder: true
                          }).then(() => {
                              this.listFiles(this.getCwd(-1)).finally()
                              this.getCreateFolderForm().hideModal()
                          }).catch(error => {
                              this.getCreateFolderForm().setError(error.errorCode, error.message)
                          })
                      }}
                      params={[
                          {
                              name: 'name',
                              title: 'Folder Name',
                              description: 'Enter the name of the folder',
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

        const performRename = async (item: FileData, newName: string) => {
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
                this.listFiles(this.getCwd(-1)).finally()
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
                header={`Rename ${filesToRename.length === 1 ? 'Item' : `${filesToRename.length} Items`}`}
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
                                    ? 'Fix Validation Errors'
                                    : changedItemsCount === 0
                                        ? 'No Changes to Apply'
                                        : `Rename ${changedItemsCount} Item${changedItemsCount === 1 ? '' : 's'}`
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
                                header: 'Current Name',
                                cell: (item: FileData) => {
                                    const filePath = this.getFilePath(item)
                                    const permissionInfo = filePermissions.get(filePath)
                                    const isProtected = permissionInfo?.is_protected
                                    const hasPermission = permissionInfo?.has_permission
                                    return (
                                        <Box fontWeight="bold">
                                            <SpaceBetween size="xs" direction="horizontal">
                                                <Box>
                                                    {item.isDir ? '📁' : '📄'}
                                                </Box>
                                                <Box>
                                                    {item.name}
                                                </Box>
                                            </SpaceBetween>
                                            {isProtected && (
                                                <Box fontSize="body-s" color="text-status-warning" margin={{top: 'xxxs'}}>
                                                    🔒 Protected - cannot be renamed
                                                </Box>
                                            )}
                                            {!isProtected && !hasPermission && (
                                                <Box fontSize="body-s" color="text-status-error" margin={{top: 'xxxs'}}>
                                                    ❌ Permission denied
                                                </Box>
                                            )}
                                        </Box>
                                    )
                                },
                                width: '50%'
                            },
                            {
                                id: 'new',
                                header: 'New Name',
                                cell: (item: FileData) => {
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
                                                placeholder={isDisabled ? "Cannot rename" : "Enter new name..."}
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



    checkRenamePermissions(selectedFiles: FileData[]) {
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
                title={"Delete File(s)"}
                onCancel={() => {
                    this.setState({
                        filesToDelete: [],
                        showDeleteConfirmModal: false
                    })
                }}
                onConfirm={() => {
                    const toDelete: string[] = []
                    this.state.filesToDelete.forEach((file) => {
                        toDelete.push(`${this.getCwd(-1)}/${file.name}`)
                    })
                    this.fileBrowserClient().deleteFiles({
                        files: toDelete
                    }).then(() => {
                        this.listFiles(this.getCwd(-1)).finally()
                    }).catch(error => {
                        if (error.errorCode === 'UNAUTHORIZED_ACCESS') {
                            this.showToast(error.errorCode, 'Permission denied')
                        } else {
                            this.showToast(error.errorCode, error.message)
                        }
                    })
                }}>
                <p>Are you sure you want to delete the following Files? </p>
                {this.state.filesToDelete.map((file, index) => {
                    return <li key={index}>{file.name}</li>
                })}
            </IdeaConfirm>
        )
    }

    deleteFiles(files: FileData[]) {
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
            AppContext.get().auth().downloadPrivateKey(keyFormat).finally(() => {
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

    render() {

        const getPath = (fileName: string): string => {
            return `${this.getCwd(-1)}/${fileName}`
        }

        const getFileBrowserActions = () => {
            let actions = [...ACTIONS]
            if (AppContext.get().getClusterSettingsService().isSchedulerDeployed()) {
                actions.push(CustomActionSubmitJob)
            }
            return actions
        }

        const getFavoriteActions = () => {
            let actions = [...FAVORITE_ACTIONS]
            if (AppContext.get().getClusterSettingsService().isSchedulerDeployed()) {
                actions.push(CustomActionSubmitJob)
            }
            return actions
        }

        return (
            <IdeaAppLayout
                ideaPageId={this.props.ideaPageId}
                header={<Header variant={"h1"}>File Browser</Header>}
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
                        text: 'Home',
                        href: '#/'
                    },
                    {
                        text: 'File Browser',
                        href: ''
                    }
                ]}
                contentType={"default"}
                disableContentHeaderOverlap={true}
                content={
                    <div style={{marginTop: '20px'}}>
                        {this.state.showDeleteConfirmModal && this.buildDeleteFileConfirmModal()}
                        {this.buildCreateFolderForm()}
                        {this.buildRenameFileModal()}
                        {this.buildFileEditor()}
                        {/*<Container>*/}
                        <Tabs
                            onChange={(event) => {
                                this.setState({
                                    activeTabId: event.detail.activeTabId
                                }, () => {
                                    this.adjustFileBrowserHeight()
                                })
                            }}
                            activeTabId={this.state.activeTabId}
                            tabs={[
                                {
                                    id: 'files',
                                    label: 'My Files',
                                    content: (
                                        <Container disableContentPaddings={true}>
                                            <div className="soca-file-browser" style={{height: '100vh - 200px'}}>
                                                <FileBrowser
                                                    darkMode={AppContext.get().isDarkMode()}
                                                    folderChain={this.state.folderChain}
                                                    files={this.state.files}
                                                    fileActions={getFileBrowserActions()}
                                                    disableDragAndDrop={true}
                                                    onFileAction={(event) => {
                                                        const eventId: string = event.id
                                                        if (event.id === ChonkyActions.OpenFiles.id) {
                                                            this.onOpenSelection(event.payload.targetFile === undefined ? event.payload.files[0]: event.payload.targetFile)
                                                        } else if (event.id === ChonkyActions.UploadFiles.id) {
                                                            this.showUploadModal()
                                                        } else if (event.id === ChonkyActions.DownloadFiles.id) {
                                                            this.downloadFiles(event.state.selectedFiles)
                                                        } else if (event.id === ChonkyActions.CreateFolder.id) {
                                                            this.getCreateFolderForm().showModal()
                                                        } else if (event.id === ChonkyActions.DeleteFiles.id) {
                                                            this.deleteFiles(event.state.selectedFiles)
                                                        } else if (event.id === ChonkyActions.CopyFiles.id) {
                                                            if (event.state.selectedFiles && event.state.selectedFiles.length > 0) {
                                                                const name = event.state.selectedFiles[0].name
                                                                const path = getPath(name)
                                                                Utils.copyToClipBoard(path).then(status => {
                                                                    if (status) {
                                                                        this.showToast(path, `${name} path copied to clipboard`, 'info')
                                                                    }
                                                                })
                                                            }
                                                        } else if (eventId === 'soca_refresh') {
                                                            this.listFiles(this.getCwd(-1)).finally()
                                                        } else if (eventId === 'soca_submit_job') {
                                                            if (event.state.selectedFiles && event.state.selectedFiles.length > 0) {
                                                                this.props.navigate(`/soca/jobs/submit-job?input_file=${this.getCwd(-1)}/${event.state.selectedFiles[0].name}`)
                                                            }
                                                        } else if (eventId === 'soca_favorite') {
                                                            if (event.state.selectedFiles && event.state.selectedFiles.length > 0) {
                                                                event.state.selectedFiles.forEach((file) => this.addFavorite(file))
                                                            }
                                                        } else if (eventId === 'soca_tail_log_file') {
                                                            if (event.state.selectedFiles && event.state.selectedFiles.length > 0) {
                                                                Utils.openNewTab(`/#/home/file-browser/tail?file=${this.getCwd(-1)}/${event.state.selectedFiles[0].name}&cwd=${this.getCwd(-1)}`)
                                                            }
                                                        } else if (eventId === 'soca_open_in_script_workbench') {
                                                            if (event.state.selectedFiles && event.state.selectedFiles.length > 0) {
                                                                this.openFileInScriptWorkbench(event.state.selectedFiles[0])
                                                            }
                                                                                                } else if (eventId === 'soca_rename_file') {
                                            if (event.state.selectedFiles && event.state.selectedFiles.length > 0) {
                                                this.checkRenamePermissions(event.state.selectedFiles)
                                            }
                                        }
                                                    }}

                                                    defaultFileViewActionId={ChonkyActions.EnableListView.id}
                                                    disableDefaultFileActions={[
                                                        ChonkyActions.CopyFiles.id,
                                                        ChonkyActions.ToggleShowFoldersFirst.id
                                                    ]}

                                                >
                                                    <FileNavbar/>
                                                    <FileToolbar/>
                                                    <FileList/>
                                                    <FileContextMenu/>
                                                </FileBrowser>
                                            </div>
                                        </Container>
                                    )
                                },
                                {
                                    id: 'favorites',
                                    label: 'Favorites',
                                    content: (
                                        <Container disableContentPaddings={true}>
                                            <div className="soca-file-browser" style={{height: '100vh - 200px'}}>
                                                <FileBrowser
                                                    darkMode={AppContext.get().isDarkMode()}
                                                    folderChain={this.state.folderChain}
                                                    files={this.state.favorites}
                                                    fileActions={getFavoriteActions()}
                                                    disableDragAndDrop={true}
                                                    onFileAction={(event) => {
                                                        const eventId: string = event.id
                                                        if (event.id === ChonkyActions.OpenFiles.id) {
                                                            this.onOpenFavorite(event.payload.targetFile)
                                                        } else if (eventId === 'soca_submit_job') {
                                                            const targetFile = event.state.selectedFiles[0]
                                                            this.props.navigate(`/soca/jobs/submit-job?input_location=${targetFile.path}`)
                                                        } else if (eventId === 'soca_remove_favorite') {
                                                            if (event.state.selectedFiles && event.state.selectedFiles.length > 0) {
                                                                event.state.selectedFiles.forEach((file) => this.removeFavorite(file))
                                                            }
                                                        }
                                                    }}
                                                    defaultFileViewActionId={ChonkyActions.EnableListView.id}
                                                    disableDefaultFileActions={[
                                                        ChonkyActions.CopyFiles.id,
                                                        ChonkyActions.ToggleShowFoldersFirst.id
                                                    ]}
                                                >
                                                    <FileToolbar/>
                                                    <FileList/>
                                                    <FileContextMenu/>
                                                </FileBrowser>
                                            </div>
                                        </Container>
                                    )
                                },
                                {
                                    id: 'file-transfer',
                                    label: 'File Transfer',
                                    disabled: !this.state.sshAccess,
                                    content: (
                                        <SpaceBetween size={"s"}>
                                            <Container>
                                                <b>File Transfer Method</b><br/>
                                                <p>We recommend using below methods to transfer large files to your IDEA cluster. Select an option below.</p>
                                                <Tiles value={this.state.fileTransferMethod}
                                                       columns={3}
                                                       items={[
                                                           {
                                                               label: <b>FileZilla</b>,
                                                               description: 'Available for download on Windows, MacOS and Linux',
                                                               value: 'file-zilla'
                                                           },
                                                           {
                                                               label: <b>WinSCP</b>,
                                                               description: 'Available for download on Windows Only',
                                                               value: 'winscp'
                                                           },
                                                           {
                                                               label: <b>AWS Transfer</b>,
                                                               description: 'Your IDEA cluster must be using Amazon EFS to use AWS Transfer',
                                                               value: 'aws-transfer'
                                                           }
                                                       ]}
                                                       onChange={(event) => {
                                                           this.setState({
                                                               fileTransferMethod: event.detail.value
                                                           })
                                                       }}
                                                />
                                            </Container>
                                            {this.state.fileTransferMethod === 'file-zilla' && <Container header={<Header variant={"h3"}>FileZilla</Header>}>
                                                <SpaceBetween size={"s"}>
                                                    <Box>
                                                        <h2>Step 1: Download FileZilla</h2>
                                                        <ul>
                                                            <li>
                                                                <Link external={true} href={"https://filezilla-project.org/download.php?platform=osx"}>Download FileZilla (MacOS)</Link>
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
                                                        <h2>Step 2: Download Key File</h2>
                                                        <SpaceBetween size={"l"} direction={"horizontal"}>
                                                            <Button variant={"normal"} onClick={() => this.onDownloadPrivateKey('pem')} loading={this.state.downloadPemLoading}><FontAwesomeIcon icon={faDownload}/> Download Key File [*.pem] (MacOS / Linux)</Button>
                                                            <Button variant={"normal"} onClick={() => this.onDownloadPrivateKey('ppk')} loading={this.state.downloadPpkLoading}><FontAwesomeIcon icon={faDownload}/> Download Key File [*.ppk] (Windows)</Button>
                                                        </SpaceBetween>
                                                    </Box>
                                                    <Box>
                                                        <h2>Step 3: Configure FileZilla</h2>
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
                                                        <h2>Step 4: Connect and transfer file to FileZilla</h2>
                                                        <p>During your first connection, you will be asked whether or not you want to trust {this.state.sshHostIp}. Check "Always Trust this Host" and Click "Ok".</p>
                                                        <p>Once connected, simply drag & drop to upload/download files.</p>
                                                    </Box>
                                                </SpaceBetween>
                                            </Container>}
                                            {this.state.fileTransferMethod === 'winscp' && <Container header={<Header variant={"h3"}>WinSCP (Windows)</Header>}>
                                                <SpaceBetween size={"s"}>
                                                    <Box>
                                                        <Alert onDismiss={() => false}
                                                               dismissAriaLabel="Close alert"
                                                               header="Info">
                                                            WinSCP is only available on Windows. Please use alternate methods (FileZilla, AWS FTP) if you are running Linux/Mac clients.
                                                        </Alert>
                                                        <h2>Step 1: Download WinSCP</h2>
                                                        <ul>
                                                            <li>
                                                                <Link external={true} href={"https://winscp.net/eng/download.php"}>Download WinSCP (Windows)</Link>
                                                            </li>
                                                        </ul>
                                                    </Box>
                                                    <Box>
                                                        <h2>Step 2: Download Key File</h2>
                                                        <SpaceBetween size={"l"} direction={"horizontal"}>
                                                            <Button variant={"normal"} onClick={() => this.onDownloadPrivateKey('pem')} loading={this.state.downloadPemLoading}><FontAwesomeIcon icon={faDownload}/> Download Key File [*.pem] (MacOS / Linux)</Button>
                                                            <Button variant={"normal"} onClick={() => this.onDownloadPrivateKey('ppk')} loading={this.state.downloadPpkLoading}><FontAwesomeIcon icon={faDownload}/> Download Key File [*.ppk] (Windows)</Button>
                                                        </SpaceBetween>
                                                    </Box>
                                                    <Box>
                                                        <h2>Step 3: Configure WinSCP</h2>
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
                                                        <h2>Step 4: Connect and transfer file to WinSCP</h2>
                                                        <p>During your first connection, you will be asked whether or not you want to trust {this.state.sshHostIp}. Check "Always Trust this Host" and Click "Ok".</p>
                                                        <p>Once connected, simply drag & drop to upload/download files.</p>
                                                    </Box>
                                                </SpaceBetween>
                                            </Container>}
                                            {this.state.fileTransferMethod === 'aws-transfer' && <Container header={<Header variant={"h3"}>AWS Transfer</Header>}>
                                                <SpaceBetween size={"s"}>
                                                    <Box>
                                                        <Alert onDismiss={() => false}
                                                               dismissAriaLabel="Close alert"
                                                               header="Limitations">
                                                            Your IDEA cluster must be using Amazon EFS to use AWS Transfer
                                                        </Alert>
                                                        <h2>Step 1: Configure AWS Transfer</h2>
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

                                                        <h2>Step 2: Create IAM role for your AWS Transfer Users</h2>
                                                        <ul>
                                                            <li>Open AWS Console and navigate to the service named <b>IAM</b> then click <b>Roles</b> on the left sidebar and finally click <b>Create Role</b></li>
                                                            <li>Select <b>AWS Service</b> as Trusted Entity Type and select <b>Transfer</b> as Use Case</li>
                                                            <li>Select the AWS managed policy named <b>AmazonElasticFileSystemClientReadWriteAccess</b></li>
                                                            <li>Select a Role name (for example <b>TransferEFSClient</b> and save it</li>
                                                        </ul>

                                                        <h2>Step 3: Download PEM Key File (Public)</h2>
                                                        <ul>
                                                            <li>Download your <b>public</b> SSH key. You can retrieve it under <b>$HOME/.ssh/id_rsa.pub</b></li>
                                                        </ul>

                                                        <h2>Step 4: Download your PEM key File (Private)</h2>
                                                        <SpaceBetween size={"l"} direction={"horizontal"}>
                                                            <Button variant={"normal"} onClick={() => this.onDownloadPrivateKey('pem')} loading={this.state.downloadPemLoading}><FontAwesomeIcon icon={faDownload}/> Download Key File [*.pem] (MacOS / Linux)</Button>
                                                        </SpaceBetween>

                                                        <h2>Step 5: Register your AWS Transfer Users</h2>
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

                                                        <h2>Step 6: Test</h2>
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
