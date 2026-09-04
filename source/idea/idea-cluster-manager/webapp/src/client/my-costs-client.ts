import {
    GetMyCostsSummaryRequest,
    GetMyCostsSummaryResult,
    GetUserCostsSummaryRequest,
    ListUserCostsRequest,
    ListUserCostsResult
} from './data-model'
import IdeaBaseClient, {IdeaBaseClientProps} from "./base-client";

export interface MyCostsClientProps extends IdeaBaseClientProps {
}

class MyCostsClient extends IdeaBaseClient<MyCostsClientProps> {

    getSummary(req: GetMyCostsSummaryRequest): Promise<GetMyCostsSummaryResult> {
        return this.apiInvoker.invoke_alt<GetMyCostsSummaryRequest, GetMyCostsSummaryResult>(
            'MyCosts.GetSummary',
            req
        )
    }

    // Admin only, enforced by the server. One row per user with a measured cost in the window.
    listUserCosts(req: ListUserCostsRequest): Promise<ListUserCostsResult> {
        return this.apiInvoker.invoke_alt<ListUserCostsRequest, ListUserCostsResult>(
            'Costs.ListUserCosts',
            req
        )
    }

    // Admin only, enforced by the server. The same summary as MyCosts.GetSummary, for the named user.
    getUserSummary(req: GetUserCostsSummaryRequest): Promise<GetMyCostsSummaryResult> {
        return this.apiInvoker.invoke_alt<GetUserCostsSummaryRequest, GetMyCostsSummaryResult>(
            'Costs.GetUserSummary',
            req
        )
    }

}

export default MyCostsClient
