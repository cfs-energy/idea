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

import {
    useLocation,
    useNavigate,
    useParams, useSearchParams
} from "react-router-dom";
import type {Location, NavigateFunction, SetURLSearchParams} from "react-router-dom";

export interface IdeaAppNavigationProps {
    navigate: NavigateFunction
    location: Location
    params: any
    searchParams: URLSearchParams
    setSearchParams: SetURLSearchParams
}

export function withRouter(Component: any) {
    function ComponentWithRouterProp(props: any) {
        let location = useLocation();
        let navigate = useNavigate();
        let params = useParams();
        let [searchParams, setSearchParams] = useSearchParams()
        return (
            <Component
                {...props}
                location={location}
                navigate={navigate}
                params={params}
                searchParams={searchParams}
                setSearchParams={setSearchParams}
            />
        );
    }

    return ComponentWithRouterProp;
}
