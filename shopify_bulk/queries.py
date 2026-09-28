"""GraphQL documents used by the toolkit (Admin API 2026-07)."""

PRODUCT_FIELDS = """
      id
      handle
      title
      status
      vendor
      productType
      tags
      updatedAt
      category { id name fullName }
"""


def bulk_products_query(search: str | None = None, metafields: bool | str = False,
                        include_variants: bool = True) -> str:
    """Bulk query for products, optionally filtered by Shopify search syntax.

    ``metafields``: False = none, True = all namespaces, "ns" = one namespace.
    """
    args = f'(query: {json_string(search)})' if search else ""
    variants = """
      variants {
        edges { node { id sku title price } }
      }""" if include_variants else ""
    mf_args = f"(namespace: {json_string(metafields)})" if isinstance(metafields, str) else ""
    metafields = f"""
      metafields{mf_args} {{
        edges {{ node {{ id namespace key type value }} }}
      }}""" if metafields else ""
    return f"""
{{
  products{args} {{
    edges {{
      node {{
{PRODUCT_FIELDS}{variants}{metafields}
      }}
    }}
  }}
}}"""


BULK_REDIRECTS = """
{
  urlRedirects {
    edges { node { id path target } }
  }
}
"""

PRODUCTS_STATE = """
query ProductsState($ids: [ID!]!) {
  nodes(ids: $ids) {
    ... on Product { id tags category { id } }
  }
}
"""

REDIRECTS_BY_PATH = """
query RedirectsByPath($query: String!) {
  urlRedirects(first: 50, query: $query) {
    edges { node { id path target } }
  }
}
"""

TAGS_ADD = """
mutation TagsAdd($id: ID!, $tags: [String!]!) {
  tagsAdd(id: $id, tags: $tags) {
    node { id }
    userErrors { field message }
  }
}
"""

TAGS_REMOVE = """
mutation TagsRemove($id: ID!, $tags: [String!]!) {
  tagsRemove(id: $id, tags: $tags) {
    node { id }
    userErrors { field message }
  }
}
"""

METAFIELDS_SET = """
mutation MetafieldsSet($metafields: [MetafieldsSetInput!]!) {
  metafieldsSet(metafields: $metafields) {
    metafields { id namespace key }
    userErrors { field message code }
  }
}
"""

PRODUCT_SET_CATEGORY = """
mutation ProductSetCategory($product: ProductUpdateInput!) {
  productUpdate(product: $product) {
    product { id category { id fullName } }
    userErrors { field message }
  }
}
"""

REDIRECT_CREATE = """
mutation RedirectCreate($urlRedirect: UrlRedirectInput!) {
  urlRedirectCreate(urlRedirect: $urlRedirect) {
    urlRedirect { id path target }
    userErrors { field message }
  }
}
"""

REDIRECT_UPDATE = """
mutation RedirectUpdate($id: ID!, $urlRedirect: UrlRedirectInput!) {
  urlRedirectUpdate(id: $id, urlRedirect: $urlRedirect) {
    urlRedirect { id path target }
    userErrors { field message }
  }
}
"""


def json_string(value: str) -> str:
    """Quote a string for inline use in a GraphQL document (bulk queries take no variables)."""
    import json

    return json.dumps(value)
