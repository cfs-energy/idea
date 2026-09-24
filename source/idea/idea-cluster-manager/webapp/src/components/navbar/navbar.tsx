import {IdeaSideNavHeader} from '../../navigation/side-nav-items';
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

import {applyDensity, applyMode, Density, Mode} from "@cloudscape-design/global-styles";
import React, {Component, RefObject} from 'react'

import {TopNavigation} from "@cloudscape-design/components";
import {AppContext} from "../../common";
import Utils from "../../common/utils";
import IdeaForm from "../form";
import {personalCostsCache} from '../../client/personal-costs-cache';
import {GetCostTickerResult} from '../../client/data-model';

export interface IdeaNavbarProps {
    logo?: IdeaNavbarLogo
    items?: IdeaNavbarItem[]
    style?: object
}

export interface IdeaNavbarState {
    darkMode: boolean
    compactMode: boolean
    showPreferences: boolean
    ticker: GetCostTickerResult | null
}

export interface IdeaNavbarItem {
    text: string
    link: string
}

export interface IdeaNavbarLogo {
    link: string
    text?: string
    beta?: boolean
    release: string
    img_url?: string
    img?: React.ReactNode
}

class IdeaNavbar extends Component<IdeaNavbarProps, IdeaNavbarState> {

    preferencesForm: RefObject<IdeaForm | null>

    constructor(props: IdeaNavbarProps) {
        super(props);
        this.preferencesForm = React.createRef()
        this.state = {
            darkMode: false,
            compactMode: false,
            showPreferences: false,
            ticker: null
        }
    }

    componentDidMount() {
        const darkMode = AppContext.get().isDarkMode()
        const compactMode = AppContext.get().isCompactMode()
        this.setState({
            'darkMode': darkMode,
            'compactMode': compactMode
        }, () => {
            this.setThemeMode(darkMode)
            this.setThemeDensity(compactMode)
        })
        this.refreshTicker()
        this.tickerPoll = setInterval(() => {
            if (document.visibilityState === 'visible') this.refreshTicker()
        }, 300000)
        document.addEventListener('visibilitychange', this.onVisibilityChange)
    }

    private disposed = false
    private unsubscribeCosts?: () => void
    private tickerPoll?: ReturnType<typeof setInterval>
    private tickerRetry?: ReturnType<typeof setTimeout>
    private tooltipTimer?: ReturnType<typeof setTimeout>

    componentWillUnmount() {
        this.disposed = true
        this.unsubscribeCosts?.()
        clearInterval(this.tickerPoll)
        clearTimeout(this.tickerRetry)
        clearTimeout(this.tooltipTimer)
        document.removeEventListener('visibilitychange', this.onVisibilityChange)
    }

    componentDidUpdate() {
        clearTimeout(this.tooltipTimer)
        this.tooltipTimer = setTimeout(this.applyTickerTooltip, 0)
    }

    applyTickerTooltip = () => {
        const ticker = this.state.ticker
        if (!ticker?.enabled || ticker.total == null) return
        const control = Array.from(document.querySelectorAll('[aria-label]'))
            .find(element => element.getAttribute('aria-label')?.startsWith(`${ticker.period} cost as of`))
        if (control) {
            control.setAttribute('title', `As of ${ticker.as_of ? new Date(ticker.as_of).toLocaleString() : 'the latest calculation'}`)
        }
    }

    onVisibilityChange = () => {
        if (document.visibilityState === 'visible') this.refreshTicker()
    }

    refreshTicker = () => {
        clearTimeout(this.tickerRetry)
        AppContext.get().client().myCosts().getCostTicker({})
            .then(ticker => {
                if (this.disposed) return
                if (!ticker.enabled || ticker.period !== 'MTD') {
                    this.unsubscribeCosts?.()
                    this.unsubscribeCosts = undefined
                }
                this.setState({ticker})
                if (ticker.enabled && ticker.period === 'MTD' && !this.unsubscribeCosts) {
                    this.unsubscribeCosts = personalCostsCache().subscribe(costs => {
                        if (costs.current) this.setState({ticker: {...ticker, total: costs.current.total,
                            currency: costs.currency, incomplete: costs.current.incomplete, as_of: costs.refreshed_at}})
                    })
                }
                if (ticker.enabled && ticker.total == null) {
                    this.tickerRetry = setTimeout(this.refreshTicker, 15000)
                }
            })
            .catch(() => {})
    }

    invokeLogout() {
        AppContext.get().auth().logout()
        AppContext.get().routeTo('/auth/login')
    }

    setThemeMode(darkMode: boolean) {
        AppContext.get().setDarkMode(darkMode)
        if (darkMode) {
            applyMode(Mode.Dark)
        } else {
            applyMode(Mode.Light)
        }
    }

    setThemeDensity(compactMode: boolean) {
        AppContext.get().setCompactMode(compactMode)
        if (compactMode) {
            applyDensity(Density.Compact)
        } else {
            applyDensity(Density.Comfortable)
        }
    }

    buildPreferencesForm = () => {
        const isDarkMode = () => {
            return this.state.darkMode
        }
        const isCompactMode = () => {
            return this.state.compactMode
        }
        return (this.state.showPreferences &&
            <IdeaForm
                ref={this.preferencesForm}
                title={"Preferences"}
                description={"Manage preferences for IDEA Web Portal"}
                name={"preferences"}
                modal={true}
                modalSize={"medium"}
                showSecondaryCta={false}
                onStateChange={(event) => {
                    if (event.param.name === 'dark_mode') {
                        const darkMode = Utils.isTrue(event.value)
                        this.setState({
                            darkMode: darkMode
                        }, () => {
                            this.setThemeMode(darkMode)
                        })
                    } else if (event.param.name === 'compact_mode') {
                        const compactMode = Utils.isTrue(event.value)
                        this.setState({
                            compactMode: compactMode
                        }, () => {
                            this.setThemeDensity(compactMode)
                        })
                    }
                }}
                onSubmit={() => {
                    this.setState({
                        showPreferences: false
                    })
                }}
                primaryCtaTitle={"Save"}
                params={[
                    {
                        name: 'dark_mode',
                        title: 'Dark Mode',
                        description: 'Toggle between Dark Mode and Light visual modes',
                        param_type: 'confirm',
                        data_type: 'bool',
                        default: isDarkMode()
                    },
                    {
                        name: 'compact_mode',
                        title: 'Compact Mode',
                        description: 'Toggle between Compact Mode and Comfortable density modes',
                        param_type: 'confirm',
                        data_type: 'bool',
                        default: isCompactMode()
                    }
                ]}
            />
        )
    }

    render() {
        const getUsername = () => {
            return AppContext.get().auth().getUsername()
        }
        const getEmail = () => {
            return AppContext.get().auth().getEmail()
        }
        const getTitle = () => {
            return AppContext.get().getTitle()
        }
        const getLogo = () => {
            let logoUrl = AppContext.get().getLogoUrl()
            if (logoUrl) {
                return logoUrl
            } else {
                return '/logo.png'
            }
        }

        const getPasswordExpirationMessage = () => {
            const expiresIn = AppContext.get().auth().getPasswordExpiresInDays()
            if(expiresIn < 0 || expiresIn > 10) {
                return
            }
            let expiryText
            if(expiresIn === 0) {
                expiryText = 'Your password has expired.'
            } else {
                if(expiresIn === 1) {
                    expiryText = `Password expires in ${expiresIn} day.`
                } else {
                    expiryText = `Password expires in ${expiresIn} days.`
                }
            }
            return expiryText
        }

        const ticker = this.state.ticker
        const tickerUtility = ticker?.enabled && ticker.total != null && ticker.currency && ticker.period ? [{
            type: 'button' as const,
            text: `${ticker.incomplete === false ? 'Estimated costs' : 'Known costs'} · ${new Intl.NumberFormat(undefined, {style: 'currency', currency: ticker.currency}).format(ticker.total)} · ${ticker.period}`,
            href: '#/home/my-costs',
            ariaLabel: `${ticker.period} cost as of ${ticker.as_of ? new Date(ticker.as_of).toLocaleString() : 'the latest calculation'}. Open My costs`
        }] : []

        let hasNotifications = false
        const getNotifications = () => {
            let passwordExpirationMessage = getPasswordExpirationMessage()

            let notifications: any = []
            if(passwordExpirationMessage) {
                hasNotifications = true
                notifications.push({
                    id: 'password-expiration',
                    text: passwordExpirationMessage,
                })
            }

            if(notifications.length === 0) {
                hasNotifications = false
                notifications.push({
                    id: 'no-op',
                    text: 'No new notifications. You are up-to-date.'
                })
            }

            return notifications
        }

        return (
            (<div>
                {this.buildPreferencesForm()}
                <TopNavigation
                    identity={{
                        href: IdeaSideNavHeader(AppContext.get()).href,
                        title: getTitle(),
                        logo: {
                            src: getLogo(),
                            alt: getTitle()
                        }
                    }}
                    utilities={[
                        {
                            type: 'menu-dropdown',
                            iconName: 'notification',
                            ariaLabel: 'Notifications',
                            badge: hasNotifications,
                            onItemClick: (event) => {
                                if(event.detail.id === 'password-expiration') {
                                    AppContext.get().routeTo('/home/account-settings')
                                }
                            },
                            items: getNotifications()
                        },
                        ...tickerUtility,
                        {
                            type: "menu-dropdown",
                            text: getUsername(),
                            /*
                             * Can also display the email instead with:
                             * Make configurable?
                            text: getEmail(),
                             */
                            iconName: "user-profile",
                            description: `${getEmail()}`,
                            onItemClick: (event) => {
                                if (event.detail.id === 'signout') {
                                    this.invokeLogout()
                                } else if (event.detail.id === 'account-settings') {
                                    AppContext.get().routeTo('/home/account-settings')
                                } else if (event.detail.id === 'preferences') {
                                    this.setState({
                                        showPreferences: true
                                    }, () => {
                                        this.preferencesForm.current!.showModal()
                                    })
                                }
                            },
                            items: [
                                {id: "account-settings", text: "My account"},
                                {id: "preferences", text: "Preferences"},
                                {
                                    id: "support-group",
                                    text: "Support",
                                    items: [
                                        {
                                            id: "documentation",
                                            text: "Documentation",
                                            href: "#",
                                            external: true,
                                            externalIconAriaLabel:
                                                " (opens in new tab)"
                                        },
                                        {
                                            id: "support",
                                            text: "Support",
                                            external: true,
                                            href: '#',
                                            externalIconAriaLabel:
                                                " (opens in new tab)"
                                        }
                                    ]
                                },
                                {id: "signout", text: "Sign out"}
                            ]
                        }
                    ]}
                    i18nStrings={{
                        searchIconAriaLabel: "Search",
                        searchDismissIconAriaLabel: "Close search",
                        overflowMenuTriggerText: "More",
                        overflowMenuTitleText: "All",
                        overflowMenuBackIconAriaLabel: "Back",
                        overflowMenuDismissIconAriaLabel: "Close menu"
                    }}/>
            </div>)

        )
    }

}

export default IdeaNavbar
