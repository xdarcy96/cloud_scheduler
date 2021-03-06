<template>
  <div class="app-container">
    <div class="filter-container" align="right">
      <!-- <el-input v-model="listQuery.title" placeholder="Title" style="width: 200px;" class="filter-item" @keyup.enter.native="handleFilter" /> -->
      <!-- <el-select v-model="listQuery.importance" placeholder="Imp" clearable style="width: 90px" class="filter-item">
        <el-option v-for="item in importanceOptions" :key="item" :label="item" :value="item" />
      </el-select>
      <el-select v-model="listQuery.type" placeholder="Type" clearable class="filter-item" style="width: 130px">
        <el-option v-for="item in calendarTypeOptions" :key="item.key" :label="item.display_name+'('+item.key+')'" :value="item.key" />
      </el-select>
      <el-select v-model="listQuery.sort" style="width: 140px" class="filter-item" @change="handleFilter">
        <el-option v-for="item in sortOptions" :key="item.key" :label="item.label" :value="item.key" />
      </el-select>
      <el-button v-waves class="filter-item" type="primary" icon="el-icon-search" @click="handleFilter">
        Search
      </el-button> -->
      <el-button v-if="permission!=='user'" class="filter-item" style="margin: 20px;" type="primary" icon="el-icon-plus" @click="handleCreate">
        New Settings
      </el-button>
    </div>

    <el-table
      :key="tableKey"
      v-loading="listLoading"
      :data="list"
      border
      fit
      highlight-current-row
      style="width: 100%;"
      @sort-change="sortChange"
    >
      <el-table-column label="UUID" width="300" align="center">
        <template slot-scope="scope">
          <span>{{ scope.row.uuid }}</span>
        </template>
      </el-table-column>
      <el-table-column label="Name" width="150" align="center">
        <template slot-scope="{row}">
          <span v-if="permission!=='user'" class="link-type" @click="handleUpdate(row)">{{ row.name }}</span>
          <span v-if="permission==='user'">{{ row.name }}</span>
        </template>
      </el-table-column>
      <el-table-column label="Description" min-width="120" align="center">
        <template slot-scope="scope">
          <span>{{ scope.row.description }}</span>
        </template>
      </el-table-column>
      <el-table-column v-if="permission==='user'" label="Time Limit" width="120" align="center">
        <template slot-scope="scope">
          <span>{{ scope.row.time_limit | timeLimitFilter }}</span>
        </template>
      </el-table-column>
      <el-table-column label="Created Time" width="220" align="center">
        <template slot-scope="scope">
          <span>{{ new Date(scope.row.create_time).toLocaleString() }}</span>
        </template>
      </el-table-column>
      <el-table-column label="Actions" align="center" :width="permission!=='user'?450:350" class-name="small-padding fixed-width">
        <template slot-scope="{row}">
          <el-button type="primary" plain size="small" @click="handleIde(row)">
            IDE
          </el-button>
          <el-button type="info" plain size="small" @click="handleSsh(row)">
            SSH
          </el-button>
          <el-button type="warning" plain size="small" @click="handleVnc(row)">
            VNC
          </el-button>
          <el-button type="success" plain size="small" icon="el-icon-plus" @click="handleAddTask(row)">
            Add Task
          </el-button>
          <el-button v-if="permission!=='user'" plain type="danger" size="small" icon="el-icon-delete" @click="handleDelete(row)">
            Delete
          </el-button>
        </template>
      </el-table-column>
    </el-table>

    <div style="text-align: center;">
      <pagination v-show="total>0" :total="total" :page.sync="listQuery.page" :limit.sync="listQuery.limit" :page-sizes="pageSizes" @pagination="getList" />
    </div>

    <el-dialog
      title="Warning"
      :visible.sync="deleteDialogVisible"
      width="30%"
    >
      <span>Are you sure to delete this settings?</span>
      <span slot="footer" class="dialog-footer">
        <el-button @click="deleteDialogVisible = false">Cancel</el-button>
        <el-button :loading="deleteDialogLoading" type="danger" @click="deleteTaskSettings">Delete</el-button>
      </span>
    </el-dialog>
  </div>
</template>

<script>
import { getTaskSettingsList, deleteTaskSettings } from '@/api/task_settings';
import { createTask } from '@/api/task';
import waves from '@/directive/waves'; // waves directive
import Pagination from '@/components/Pagination'; // secondary package based on el-pagination
import { mapGetters } from 'vuex';
import { Base64 } from 'js-base64';

export default {
    name: 'TaskSettings',
    components: { Pagination },
    directives: { waves },
    filters: {
        timeLimitFilter(value) {
            return value + ' s';
        }
    },
    data() {
        const validConfig = (rule, value, callback) => {
            try {
                JSON.parse(value);
                callback();
            } catch {
                callback(new Error('Invalid config'));
            }
        };
        return {
            dialogType: 'Create',
            dialogFormVisible: false,
            deleteDialogLoading: false,
            deleteDialogVisible: false,
            tableKey: 0,
            list: null,
            total: 0,
            listLoading: true,
            pageSizes: [25],
            listQuery: {
                page: 1,
                limit: 25
            },
            uuidSelected: undefined,
            dialogRules: {
                name: [{
                    required: true,
                    message: 'name is required',
                    trigger: 'change'
                }],
                concurrency: [{
                    required: true,
                    message: 'concurrency is required'
                }],
                task_config: [{
                    required: true,
                    message: 'invalid config',
                    trigger: 'change',
                    validator: validConfig
                }]
            }
        };
    },
    computed: {
        ...mapGetters([
            'permission',
            'name',
            'token'
        ])
    },
    created() {
        this.getList();
    },
    methods: {
        getList() {
            this.listLoading = true;

            getTaskSettingsList(this.listQuery.page).then(response => {
                this.list = response.payload.entry;
                this.total = response.payload.count;
            }).finally(() => {
                this.listLoading = false;
            });
        },
        handleCreate() {
            this.$router.push({ name: 'task-settings-detail' });
        },
        handleUpdate(row) {
            this.$router.push({ name: 'task-settings-detail', query: { settings_uuid: row.uuid }});
        },
        handleDelete(row) {
            this.deleteDialogVisible = true;
            this.dialogData = Object.assign({}, row);
        },
        handleSsh(row) {
            const routeData = this.$router.resolve({
                name: 'user-terminal',
                query: { identity: Base64.encode(
                    JSON.stringify({ username: this.name, token: this.token, uuid: row.uuid })
                ) }
            });
            window.open(routeData.href, '_blank');
        },
        handleIde(row) {
            this.$router.push({ name: 'webide', query: { uuid: row.uuid }});
        },
        handleVnc(row) {
            this.$router.push({ name: 'vnc', query: { uuid: row.uuid }});
        },
        deleteTaskSettings() {
            this.deleteDialogLoading = true;
            deleteTaskSettings(this.dialogData.uuid).then(response => {
                this.$message({
                    message: 'Task Settings Deleted',
                    type: 'success'
                });
                this.getList();
            }).finally(() => {
                this.deleteDialogLoading = false;
                this.deleteDialogVisible = false;
            });
        },
        handleDialogConfirm() {
            this.$refs.dialogForm.validate(valid => {
                if (valid) {
                    if (this.dialogType === 'Create') {
                        this.createTaskSettings();
                    } else {
                        this.updateTaskSettings();
                    }
                } else {
                    return false;
                }
            });
        },
        handleAddTask(row) {
            createTask(row.uuid).then(response => {
                this.$message({
                    message: 'Task Created',
                    type: 'success'
                });
            });
        },
        sortChange(data) {
            const { prop, order } = data;
            console.log(prop);
            console.log(order);
        }
    }
};
</script>

<style lang="scss" scoped>
.link-type,
.link-type:focus {
  color: #337ab7;
  cursor: pointer;

  &:hover {
    color: rgb(32, 160, 255);
  }
}
</style>
