// Licensed to the Apache Software Foundation (ASF) under one
// or more contributor license agreements.  See the NOTICE file
// distributed with this work for additional information
// regarding copyright ownership.  The ASF licenses this file
// to you under the Apache License, Version 2.0 (the
// "License"); you may not use this file except in compliance
// with the License.  You may obtain a copy of the License at
//
//   http://www.apache.org/licenses/LICENSE-2.0
//
// Unless required by applicable law or agreed to in writing,
// software distributed under the License is distributed on an
// "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
// KIND, either express or implied.  See the License for the
// specific language governing permissions and limitations
// under the License.
#pragma once

#include <stdint.h>

#include "common/be_mock_util.h"
#include "common/status.h"
#include "operator.h"
#include "pipeline/exec/join_probe_operator.h"

namespace doris {
class RuntimeState;

namespace pipeline {
#include "common/compile_check_begin.h"
class HashJoinProbeLocalState;

using HashTableCtxVariants =
        std::variant<std::monostate, ProcessHashTableProbe<TJoinOp::INNER_JOIN>,
                     ProcessHashTableProbe<TJoinOp::LEFT_SEMI_JOIN>,
                     ProcessHashTableProbe<TJoinOp::LEFT_ANTI_JOIN>,
                     ProcessHashTableProbe<TJoinOp::LEFT_OUTER_JOIN>,
                     ProcessHashTableProbe<TJoinOp::FULL_OUTER_JOIN>,
                     ProcessHashTableProbe<TJoinOp::RIGHT_OUTER_JOIN>,
                     ProcessHashTableProbe<TJoinOp::RIGHT_SEMI_JOIN>,
                     ProcessHashTableProbe<TJoinOp::RIGHT_ANTI_JOIN>,
                     ProcessHashTableProbe<TJoinOp::NULL_AWARE_LEFT_ANTI_JOIN>,
                     ProcessHashTableProbe<TJoinOp::NULL_AWARE_LEFT_SEMI_JOIN>>;

class HashJoinProbeOperatorX;
class HashJoinProbeLocalState MOCK_REMOVE(final)
        : public JoinProbeLocalState<HashJoinSharedState, HashJoinProbeLocalState> {
public:
    using Parent = HashJoinProbeOperatorX;
    ENABLE_FACTORY_CREATOR(HashJoinProbeLocalState);
    HashJoinProbeLocalState(RuntimeState* state, OperatorXBase* parent);
    ~HashJoinProbeLocalState() override = default;

    Status init(RuntimeState* state, LocalStateInfo& info) override;
    Status open(RuntimeState* state) override;
    Status close(RuntimeState* state) override;

    void prepare_for_next();
    Status filter_data_and_build_output(RuntimeState* state, vectorized::Block* output_block,
                                        bool* eos, vectorized::Block* temp_block,
                                        bool check_rows_count = true);

    bool has_null_in_build_side() { return _shared_state->_has_null_in_build_side; }
    const std::shared_ptr<vectorized::Block>& build_block() const {
        return _shared_state->build_block;
    }
    bool empty_right_table_shortcut() const {
        return _shared_state->empty_right_table_need_probe_dispose;
    }
    std::string debug_string(int indentation_level) const override;

private:
    void _prepare_probe_block();
    bool _need_probe_null_map(vectorized::Block& block, const std::vector<int>& res_col_ids);
    std::vector<uint16_t> _convert_block_to_null(vectorized::Block& block);
    Status _extract_join_column(vectorized::Block& block, const std::vector<int>& res_col_ids);
    friend class HashJoinProbeOperatorX;
    template <int JoinOpType>
    friend struct ProcessHashTableProbe;

    int _probe_index = -1;
    uint32_t _build_index = 0;
    bool _ready_probe = false;
    bool _probe_eos = false;
    int _last_probe_match;
    // For mark join, last probe index of null mark
    int _last_probe_null_mark;

    vectorized::Block _probe_block;
    vectorized::ColumnRawPtrs _probe_columns;
    // other expr
    vectorized::VExprContextSPtrs _other_join_conjuncts;

    vectorized::VExprContextSPtrs _mark_join_conjuncts;

    std::vector<vectorized::ColumnPtr> _key_columns_holder;

    // probe expr
    vectorized::VExprContextSPtrs _probe_expr_ctxs;
    std::vector<uint16_t> _probe_column_disguise_null;
    std::vector<uint16_t> _probe_column_convert_to_null;

    bool _need_null_map_for_probe = false;
    bool _has_set_need_null_map_for_probe = false;
    vectorized::ColumnUInt8::MutablePtr _null_map_column;
    std::unique_ptr<HashTableCtxVariants> _process_hashtable_ctx_variants =
            std::make_unique<HashTableCtxVariants>();

    int _task_idx;

    RuntimeProfile::Counter* _probe_expr_call_timer = nullptr;
    RuntimeProfile::Counter* _probe_side_output_timer = nullptr;
    RuntimeProfile::HighWaterMarkCounter* _probe_arena_memory_usage = nullptr;
    RuntimeProfile::Counter* _search_hashtable_timer = nullptr;
    RuntimeProfile::Counter* _init_probe_side_timer = nullptr;
    RuntimeProfile::Counter* _build_side_output_timer = nullptr;
    RuntimeProfile::Counter* _non_equal_join_conjuncts_timer = nullptr;
};

class HashJoinProbeOperatorX MOCK_REMOVE(final)
        : public JoinProbeOperatorX<HashJoinProbeLocalState> {
public:
    HashJoinProbeOperatorX(ObjectPool* pool, const TPlanNode& tnode, int operator_id,
                           const DescriptorTbl& descs);
    Status init(const TPlanNode& tnode, RuntimeState* state) override;
    Status prepare(RuntimeState* state) override;

    Status push(RuntimeState* state, vectorized::Block* input_block, bool eos) const override;
    Status pull(doris::RuntimeState* state, vectorized::Block* output_block,
                bool* eos) const override;

    bool need_more_input_data(RuntimeState* state) const override;
    DataDistribution required_data_distribution() const override {
        if (_join_op == TJoinOp::NULL_AWARE_LEFT_ANTI_JOIN) {
            return {ExchangeType::NOOP};
        }
        return _is_broadcast_join
                       ? DataDistribution(ExchangeType::PASSTHROUGH)
                       : (_join_distribution == TJoinDistributionType::BUCKET_SHUFFLE ||
                                          _join_distribution == TJoinDistributionType::COLOCATE
                                  ? DataDistribution(ExchangeType::BUCKET_HASH_SHUFFLE,
                                                     _partition_exprs)
                                  : DataDistribution(ExchangeType::HASH_SHUFFLE, _partition_exprs));
    }
    bool is_broadcast_join() const { return _is_broadcast_join; }

    bool is_shuffled_operator() const override {
        return _join_distribution == TJoinDistributionType::PARTITIONED;
    }
    bool require_data_distribution() const override {
        return _join_distribution != TJoinDistributionType::BROADCAST &&
               _join_distribution != TJoinDistributionType::NONE;
    }

    bool need_finalize_variant_column() const { return _need_finalize_variant_column; }

    bool can_do_lazy_materialized() const { return _have_other_join_conjunct || _is_mark_join; }

    bool is_lazy_materialized_column(int column_id) const {
        return can_do_lazy_materialized() &&
               !_should_not_lazy_materialized_column_ids.contains(column_id);
    }

private:
    Status _do_evaluate(vectorized::Block& block, vectorized::VExprContextSPtrs& exprs,
                        RuntimeProfile::Counter& expr_call_timer,
                        std::vector<int>& res_col_ids) const;
    friend class HashJoinProbeLocalState;
    template <int JoinOpType>
    friend struct ProcessHashTableProbe;

    const TJoinDistributionType::type _join_distribution;

    const bool _is_broadcast_join;
    // other expr
    vectorized::VExprContextSPtrs _other_join_conjuncts;

    vectorized::VExprContextSPtrs _mark_join_conjuncts;
    // mark the build hash table whether it needs to store null value
    std::vector<bool> _serialize_null_into_key;

    // probe expr
    vectorized::VExprContextSPtrs _probe_expr_ctxs;

    vectorized::DataTypes _right_table_data_types;
    vectorized::DataTypes _left_table_data_types;
    std::vector<SlotId> _hash_output_slot_ids;
    std::vector<bool> _left_output_slot_flags;
    std::vector<bool> _right_output_slot_flags;
    bool _need_finalize_variant_column = false;
    std::set<int> _should_not_lazy_materialized_column_ids;
    std::vector<std::string> _right_table_column_names;
    const std::vector<TExpr> _partition_exprs;

    // Index of column(slot) from right table in the `_intermediate_row_desc`.
    size_t _right_col_idx;
};

} // namespace pipeline
} // namespace doris
#include "common/compile_check_end.h"