import {
    GetMyCostsResult,
    GetCostTickerRequest,
    GetCostTickerResult,
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

    getCostTicker(req: GetCostTickerRequest = {}): Promise<GetCostTickerResult> {
        return this.apiInvoker.invoke_alt<GetCostTickerRequest, GetCostTickerResult>('MyCosts.GetCostTicker', req)
    }

    refresh(): Promise<GetMyCostsResult> {
        return this.apiInvoker.invoke_alt('MyCosts.Refresh', {})
    }

    getCosts(req: GetMyCostsSummaryRequest): Promise<GetMyCostsResult> {
        return this.apiInvoker.invoke_alt<GetMyCostsSummaryRequest, GetMyCostsResult>('MyCosts.GetCosts', req)
    }

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

    // Admin only. Calendar-month costs from the same cached service as the personal billboard.
    getUserCosts(req: GetUserCostsSummaryRequest): Promise<GetMyCostsResult> {
        return this.apiInvoker.invoke_alt<GetUserCostsSummaryRequest, GetMyCostsResult>(
            'Costs.GetUserCosts',
            req
        )
    }

}

export default MyCostsClient
