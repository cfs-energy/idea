import React, {Component} from "react";
import {Header} from "@cloudscape-design/components";
import {GetMyCostsSummaryResult} from "../../client/data-model";
import {IdeaSideNavigationProps} from "../../components/side-navigation";
import IdeaAppLayout, {IdeaAppLayoutProps} from "../../components/app-layout";
import CostSections, {ESTIMATED_NOTE} from "../../components/cost-sections";
import {withRouter} from "../../navigation/navigation-utils";
import {AppContext} from "../../common";

export interface MyCostsProps extends IdeaAppLayoutProps, IdeaSideNavigationProps {
}

export interface MyCostsState {
    summary: GetMyCostsSummaryResult | null
    error: string | null
}

class MyCosts extends Component<MyCostsProps, MyCostsState> {

    constructor(props: MyCostsProps) {
        super(props);
        this.state = {summary: null, error: null}
    }

    componentDidMount() {
        this.fetch().finally()
    }

    fetch(): Promise<boolean> {
        return new Promise<boolean>((resolve) => {
            AppContext.get().client().myCosts().getSummary({}).then((result) => {
                this.setState({summary: result, error: null}, () => resolve(true))
            }).catch((e) => {
                const message = e?.message ?? `${e}`
                this.setState({summary: {} as GetMyCostsSummaryResult, error: message}, () => {
                    this.props.onFlashbarChange({
                        items: [{type: 'error', header: 'Failed to load your costs', content: message, dismissible: true}]
                    })
                    resolve(false)
                })
            })
        })
    }

    retry() {
        this.setState({summary: null, error: null}, () => this.fetch().finally())
    }

    render() {
        return (
            <IdeaAppLayout
                ideaPageId={this.props.ideaPageId}
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
                    {text: 'IDEA', href: '#/'},
                    {text: 'Home', href: '#/'},
                    {text: 'My Costs', href: ''}
                ]}
                header={
                    <Header
                        variant="h1"
                        description={`${ESTIMATED_NOTE} Window: the last 30 days.`}>
                        My Costs
                    </Header>
                }
                contentType={"default"}
                content={
                    <CostSections
                        summary={this.state.summary}
                        loading={this.state.summary === null}
                        error={this.state.error}
                        onRetry={() => this.retry()}
                        subject="self"
                    />
                }/>
        )
    }
}

export default withRouter(MyCosts)
