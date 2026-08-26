export interface HcmObjectItem {
  class_: string;
  name: string;
  description: string | null;
  model_category: string;
  i18n: boolean;
  id: string;
  update_time: string | null;
  [k: string]: any;
}

export interface HcmObjectListResult {
  list: HcmObjectItem[];
  total?: number;
  count?: number;
  [k: string]: any;
}

export interface HcmFieldMeta {
  key: string;
  name: string;
  type: string;
  description: string | null;
  length?: number | null;
  precision?: number | null;
  is_required?: boolean;
  is_list?: boolean;
  is_list_display?: boolean;
  is_info?: boolean;
  is_blur?: boolean;
  format?: string | null;
  is_logic?: boolean;
  [k: string]: any;
}

export interface HcmModelMeta {
  property?: Record<string, any>;
  persistence_table?: string | null;
  fields: HcmFieldMeta[];
  validators?: any[];
  plugins?: any[];
  childrens?: any[];
  description?: string | null;
  model_guidelines?: any;
  i18n?: any;
  extend?: any;
  model_category?: string;
  include?: any;
  roles?: any;
  rules?: any;
  action?: any;
  meta_key?: string;
  model?: string;
  state?: any;
  [k: string]: any;
}
