[Skip to main content](https://developer.mappls.com/mapping/nearby-api/#)

MapmyIndia Search APIs unleash the full potential of MapmyIndia Maps to provide users with ultimate location discoverability experience. It helps users explore our comprehensive & accurate map with over 50 million places mapped for India & its neighbouring nations.

Nearby Places API, enables you to add discovery and search of nearby POIs by searching for a generic keyword used to describe a category of places or via the unique code assigned to that category.

## Nearby API  ```  1.0.0  ```    ``` OAS3 ```

[https://mappls-api.github.io/mappls-rest-apis/custom/nearby.yml](https://mappls-api.github.io/mappls-rest-apis/custom/nearby.yml)

Before using the API in the your solution, please ensure that the related access is enabled in the [Mappls Console](https://auth.mappls.com/console/), within your app - be it for Mobile OR Web or Cloud integration.

1. Copy and paste the key from your `credentials` section from your API [keys](https://auth.mappls.com/console/) into the `access_token` query parameter.

   - Your static key can be secured by whitelisting its usage for particular IPs (in case of cloud app usage) OR a set of domains (in case of a web app).

   - Your static key obtained from your Console is to be passed as a query parameter: `access_token`.

**Note**:

1. The response listed in the below documentation is ONLY indicative of the overall capabilities of Mappls's Search APIs.
2. Not all response parameters mentioned in the below documentation is assured to be present in all the use-cases. The response of any of the below search API is thus, dependent on the use-case agreed upon between Mappls & it's API consumer.
3. For any further clarifications on what all of the response structure is available for your use case, please contact your business relationship manager or contact Mappls API support.
4. **PREMIUM APIs/Parameters** are not available for evalulation on signup. To get access, please contact API Support.

[Terms of service](https://about.mappls.com/api/terms-&-conditions)

[Mappls API Support - Website](https://about.mappls.com/api/)

[Send email to Mappls API Support](mailto:apisupport@mappls.com)

[Mappls License Key Signup](https://about.mappls.com/api/)

Servers

https://search.mappls.com - Live Prod server for APIs

Authorize

### Nearby API

GET/api/places/nearby/json
Nearby API

#### Schemas

tokenizeAddress

Nearby Results

Suggested Locations

Page Info

Keywords